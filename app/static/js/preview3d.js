/* Vista previa del cortante: el visor 3D y la foto cenital descargable.
 *
 * Las dos vistas cargan el `.glb` que produjo el mismo pipeline que el `.3mf`:
 * no es una aproximacion ni una reconstruccion, es **la misma geometria**
 * exportada en otro formato. Lo que se ve girando —y lo que sale en el JPG— es
 * lo que se descarga.
 *
 * El GLB se pide UNA sola vez y las dos vistas se suscriben al resultado
 * (`cuandoCargue`). Cada una recibe su propio clon del grafo, y eso resuelve
 * dos cosas de una: `Object3D.clone()` **comparte geometrias y materiales**,
 * asi que la malla no se duplica en memoria y pintar la pieza en una vista la
 * pinta en la otra sin una sola linea de sincronizacion. El color de la pieza
 * es uno, no dos — lo que separa a las vistas es la camara y la luz.
 *
 * La fidelidad del aspecto sale de tres cosas:
 *
 * 1. **El acabado viene en el archivo.** El motor le asigna PBR a cada objeto
 *    (metallic 0, roughness 0,62 — PLA mate) y eso no se toca. Lo unico que
 *    se pisa es el **color**, que lo elige la paleta de la pantalla: la pieza
 *    se imprime en un filamento, asi que el celeste y el gris que trae el
 *    archivo son una convencion del motor, no una propiedad de la pieza.
 * 2. **Un entorno de estudio generado por codigo.** Sin `environment`, un
 *    material PBR mate se ve como plastilina plana. Se arma una caja de
 *    paneles emisivos y se convierte con `PMREMGenerator`, que da los
 *    reflejos suaves del plastico sin vendorizar ningun HDRI.
 * 3. **Tone mapping ACES y espacio sRGB**, mas una luz principal con sombra
 *    apoyada en el piso, que es lo que da la nocion de volumen y de apoyo.
 *
 * El GLB de trimesh viene en Z arriba (se comprobo: el nodo no trae
 * transformacion), asi que el modelo se rota -90 grados en X al cargarlo. Un
 * detalle de esa rotacion lo necesita la camara cenital: deja el **arriba del
 * arte original apuntando al -Z del mundo**, y esa es la vertical de la foto.
 *
 * ⚠ El vendor respeta la disposicion de `three/examples/jsm` **a proposito**:
 * `GLTFLoader.js` importa `../utils/BufferGeometryUtils.js` y
 * `../utils/SkeletonUtils.js`, y esos imports relativos solo resuelven si los
 * archivos estan donde upstream los pone. Aplanar el directorio rompe el grafo
 * de modulos entero — y en silencio, porque un import que 404ea no tira ningun
 * error visible: simplemente este archivo nunca se evalua.
 */

import * as THREE from 'three';
import { GLTFLoader } from '/static/vendor/three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from '/static/vendor/three/addons/controls/OrbitControls.js';

/* ── Constantes de la vista imagen ───────────────────────────────────────── */

/* El lado del JPG, en orden de preferencia. 2048 px es el tope seguro de
   `max_texture_size` en cualquier GPU que corra WebGL y alcanza para imprimir
   la foto en A5; el segundo valor solo entra si el primero no logra bajar de
   los 2 MB, que con una foto de fondo liso no deberia pasar nunca. */
const LADOS_JPG = [2048, 1536];

/* "En lo posible no debe superar los 2MB": se prueba de mayor a menor calidad
   y se devuelve la primera que entra. Si ninguna entra —no se vio pasar— se
   baja la mas chica: un archivo un poco mas grande es mejor que ninguno. */
const TOPE_BYTES = 2 * 1024 * 1024;
const CALIDADES_JPG = [0.92, 0.86, 0.78, 0.7, 0.6];

/* Campo visual de la camara cenital.
 *
 * 32 grados es el punto medio que se busco: con menos, la proyeccion se vuelve
 * casi ortografica y la pieza se ve como un recorte plano; con mas, las
 * paredes del filo se abren tanto hacia los bordes que el dibujo se deforma.
 * Asi, el filo se ve de canto solo cerca de los bordes del cuadro —que es
 * exactamente lo que hace una foto de producto tomada a plomo con un lente
 * normal— y el centro sale sin distorsion. */
const FOV_JPG = 32;

/* Direccion de la luz principal — la del VISOR, y de ahi el largo de la sombra.
 *
 * Es el mismo vector que usa `agregarLuces`, declarado aca arriba para que no
 * haya dos verdades: **el encuadre de la foto depende de el**. La sombra se
 * corre `SOMBRA_POR_MM` milimetros por cada milimetro de alto de la pieza, y
 * si el cuadro no la contempla sale cortada.
 *
 * Que salga del visor y no de un vector propio es justamente el pedido: la
 * foto tiene que verse igual que la vista 3D, y la direccion de la luz —con
 * su sombra— es la mitad de eso. La otra mitad es el reparto de paneles, que
 * SI cambia entre las dos vistas: ver la nota de `ESTUDIO_ORBITA`.
 *
 * ⚠ Va ACA ARRIBA y no al lado de `agregarLuces`, que seria su lugar natural,
 * por la misma razon que `ESTUDIO_ORBITA` (ver su nota): las dos vistas se
 * inicializan mas arriba que el final del archivo, y un `const` declarado
 * despues esta en zona muerta temporal. Una funcion se hubiera hoisteado; un
 * `const` no.
 */
const DIR_PRINCIPAL = [70, 130, 90];
const SOMBRA_POR_MM = Math.hypot(DIR_PRINCIPAL[0], DIR_PRINCIPAL[2]) / DIR_PRINCIPAL[1];

/* Lugar maximo que se le cede a la sombra, en veces el radio de la pieza. No
   ata en ninguna proporcion normal —una pieza de 90 mm y 14 mm de alto pide
   0,14— y solo entra a jugar en las muy altas para su huella. */
const TOPE_SOMBRA = 0.4;

/* Aire alrededor de lo que hay que meter en el cuadro — pieza MAS sombra.
 *
 * Es chico (6%) a proposito: el lugar de la sombra ya esta contado aparte, asi
 * que esto es solo aire. Verificado con la matematica de three corriendo fuera
 * del navegador, sobre cinco proporciones (cuadrada, panoramica, alta, chata
 * de 3 mm y alta de 40 mm): en las cinco entra la pieza Y su sombra, y la
 * pieza proyecta exactamente en el centro.
 *
 * Ojo con dos consecuencias que NO son bugs: con el cuadro cuadrado, una pieza
 * panoramica llena el ancho y deja franja de fondo arriba y abajo (el recorte
 * alternativo seria cortarle el dibujo); y una pieza muy alta para su huella
 * sale mas chica en el cuadro, porque su sombra es casi tan grande como ella.
 */
const MARGEN_JPG = 1.06;

/* Los paneles de la caja de estudio: intensidad y color de cada uno.
 *
 * **Son dos repartos y la diferencia es una sola idea:** el visor mira la
 * pieza en angulo y la foto la mira a plomo. Un panel cenital fuerte es lo que
 * le da volumen a la pieza mientras gira; visto a plomo ese mismo panel cae
 * por igual sobre el plato y sobre el fondo del surco, y como nada lo tapa ahi
 * adentro —no hay oclusion ambiental— el grabado se borra.
 *
 * Medido con el modelo del estudio corrido fuera del navegador, sobre la pieza
 * Rosa vista a plomo (niveles de luminancia sRGB, de 0 a 255):
 *
 *   reparto          plato   plato-surco   plato-pared   gris neutro
 *   ESTUDIO_ORBITA     221        9,5          34,1      pared azulada
 *   ESTUDIO_FOTO       193       29,1          31,2      neutro
 *
 * El grabado pasa de 9,5 niveles de contraste a 29,1 —de no leerse a leerse—
 * y las paredes conservan los suyos. Lo que se paga es que la pieza deja de
 * salir 72 niveles por encima de su propio color, que era justamente lo que
 * la hacia ver lavada.
 *
 * `frio` y `calido` son los dos paneles laterales. El visor los tiene
 * tinteados a proposito (azul de un lado, ambar del otro: es lo que evita que
 * un plastico mate parezca plastilina mientras gira). La foto los pone
 * **blancos**: es un archivo que el usuario se lleva, y el color de la pieza
 * tiene que ser el que eligio, no el que le puso la caja de luces.
 *
 * ⚠ Viven ACA ARRIBA y no al lado de `crearEntorno`, que seria su lugar
 * natural: `crearEntorno` toma uno como valor por defecto de un parametro, y
 * las dos vistas se inicializan mas arriba que el final del archivo. Un
 * `const` declarado despues esta en zona muerta temporal, asi que la llamada
 * tiraba `ReferenceError` — y lo peor es que no se veia: el `try` del arranque
 * se lo comia y la pantalla decia "este navegador no puede generar la imagen".
 * Una funcion se hubiera hoisteado; un `const` no.
 */
const ESTUDIO_ORBITA = {
  cenital: 3.2, lateral: 1.5, contra: 0.9, piso: 1.0, frio: 0xd8ecf7, calido: 0xfff0dd,
};
const ESTUDIO_FOTO = {
  cenital: 0.8, lateral: 1.3, contra: 1.3, piso: 0.5, frio: 0xffffff, calido: 0xffffff,
};

/* Cuanta luz SIN DIRECCION queda en la foto: el hemisferico y el relleno.
 *
 * Es la otra mitad de `ESTUDIO_FOTO` y obedece al mismo razonamiento: toda luz
 * que llega de todos lados por igual entra tambien al fondo del surco, y lo
 * que no se distingue no es el grabado sino la diferencia entre el grabado y
 * el plato. Bajarla es lo que hace que la sombra propia del relieve valga.
 */
const AMBIENTE_FOTO = { hemisferico: 0.35, relleno: 0.45 };

/* La exposicion, una sola para las dos vistas.
 *
 * Vive en una constante y no repetida en cada vista porque es la perilla
 * GLOBAL: moverla sube o baja todo por igual, el plato y el fondo del surco,
 * asi que no sirve para recuperar el relieve —eso lo hace el reparto de
 * paneles— y si sirve para que las dos vistas no se separen sin querer. */
const EXPOSICION_VISOR = 1.05;


/* ── Ganchos de la pantalla ──────────────────────────────────────────────── */

const contenedor = document.getElementById('visor');
const lienzo = document.getElementById('lienzo');

/**
 * La muestra marcada de una paleta, o `null` si esa paleta no esta en pantalla.
 *
 * Devuelve el BOTON y no su color porque del fondo se leen dos cosas del mismo
 * elemento: el hex y si ese fondo lleva piso (`data-sin-piso`). Separarlas en
 * dos consultas abriria la posibilidad de que una mire una muestra y la otra
 * mire otra.
 */
function muestraElegida(selector) {
  return document.querySelector(`${selector} .paleta__color[aria-pressed="true"]`);
}

/**
 * El color elegido sale de la propia paleta de la pantalla.
 *
 * No hay una segunda lista de colores aca: la unica esta en `COLORES` del
 * router, el template dibuja las paletas y este modulo lee la muestra marcada.
 * Si la paleta no existe devuelve `null`, y quien llama decide la degradacion
 * correcta —la pieza se queda con los materiales del motor, el fondo con el
 * gris neutro de abajo.
 */
function colorElegido(selector) {
  const muestra = muestraElegida(selector);
  return muestra ? muestra.dataset.color : null;
}

function pista(id, texto) {
  const el = document.getElementById(id);
  if (el) el.textContent = texto;
}

/**
 * Avisa como termino la carga del visor 3D.
 *
 * La pantalla lo usa para el texto que acompaña a las descargas: si la pieza
 * se ve, dice que eso es exactamente lo que se baja. Se emite tambien cuando
 * falla, con `ok: false`. Las descargas NO dependen de esto —se habilitan al
 * terminar el trabajo—: un visor roto no dice nada del archivo.
 */
function avisarPreview(ok) {
  document.dispatchEvent(new CustomEvent('cortante:preview', { detail: { ok } }));
}

/**
 * Pinta la pieza entera del color elegido, sin tocar el acabado.
 *
 * Los dos cuerpos van del mismo color a proposito: es una pieza impresa en un
 * filamento. Dejarlos de colores distintos —como vienen del motor— sugiere un
 * bicolor que la impresora no hace sola.
 *
 * Como los clones comparten material con el original, pintar cualquiera de las
 * dos vistas pinta las dos. Es intencional: el color de la pieza es uno.
 */
function pintarPieza(raiz, color) {
  if (!raiz || !color) return;
  raiz.traverse((o) => {
    if (!o.isMesh) return;
    const materiales = Array.isArray(o.material) ? o.material : [o.material];
    materiales.forEach((m) => {
      if (m && m.color) m.color.set(color);
    });
  });
}

/* ── Puente con `app.js` para exportar la foto ───────────────────────────── */

/**
 * Lo que la vista imagen deja para que la invoquen, o `null` si no hay vista.
 *
 * `app.js` es script clasico y este archivo es modulo: **no pueden importarse**
 * y el unico canal es el bus de `CustomEvent` sobre `document`. Que la funcion
 * viva en una variable de modulo —y no adentro del listener— es lo que permite
 * lo de abajo: el listener se registra SIEMPRE, incluso si `iniciarImagen`
 * tiro por falta de WebGL.
 */
let exportarFoto = null;

/**
 * Aviso sobre el estado de la foto. Dos motivos, y hay que distinguirlos.
 *
 * - `'carga'`: el modelo cargo (o no). Sirve para decidir si la entrada del
 *   JPG se ofrece, y llega sola cada vez que se genera un cortante.
 * - `'exportar'`: la respuesta a un `cortante:exportar` concreto.
 *
 * ⚠ **Sin el motivo, quien espera una exportacion se come el aviso de carga.**
 * Si un `.glb` termina de cargar justo entre el clic y la respuesta del `PUT`,
 * la promesa de `pedirFoto` resolvia antes de tiempo y la descarga arrancaba
 * con la subida todavia en vuelo — o sea un ZIP con la foto vieja, que es
 * exactamente lo que este diseño existe para evitar.
 */
function avisarImagen(ok, error = null, motivo = 'carga') {
  if (error) pista('pista-imagen', error);
  document.dispatchEvent(new CustomEvent('cortante:imagen', { detail: { ok, error, motivo } }));
}

/* El listener va ACA ARRIBA y no adentro de `iniciarImagen` a proposito.
 *
 * Si estuviera adentro, un navegador sin WebGL —donde `iniciarImagen` tira y
 * el `try` del arranque se lo come— dejaria el evento sin nadie escuchando, y
 * `app.js` se quedaria esperando una respuesta que no llega nunca. La
 * alternativa seria un timeout del otro lado, que es adivinar. Asi siempre hay
 * alguien que contesta, aunque la respuesta sea que no se puede. */
document.addEventListener('cortante:exportar', (e) => {
  if (!exportarFoto) {
    avisarImagen(false, 'este navegador no puede generar la imagen', 'exportar');
    return;
  }
  exportarFoto(e.detail.destino);
});

/** Centra el objeto en X/Z y lo apoya en y = 0. Devuelve su tamaño. */
function apoyar(objeto) {
  const caja = new THREE.Box3().setFromObject(objeto);
  const tamano = caja.getSize(new THREE.Vector3());
  const centro = caja.getCenter(new THREE.Vector3());
  objeto.position.x -= centro.x;
  objeto.position.z -= centro.z;
  objeto.position.y -= caja.min.y;
  return tamano;
}

/* ── Un solo pedido del GLB para las dos vistas ──────────────────────────── */

const oyentes = [];
let cargado = null;

/**
 * Suscribe una vista al `.glb` del trabajo.
 *
 * Se registra ANTES de que llegue el resultado (al inicializar cada vista) y
 * tambien sirve despues: si el modelo ya esta cargado, el suscriptor nuevo
 * recibe su clon en el acto. Asi la vista imagen puede inicializarse tarde
 * —cuando el usuario la abre por primera vez— sin volver a pedir el archivo.
 */
function cuandoCargue(alListo, alFallar) {
  oyentes.push({ alListo, alFallar });
  if (cargado) alListo(cargado.clone());
}

document.addEventListener('cortante:listo', (e) => {
  // Sin ninguna vista viva no hay nada que cargar: el archivo ya esta
  // descargable de todas formas.
  if (!oyentes.length) return;
  new GLTFLoader().load(
    e.detail.url,
    (gltf) => {
      const anterior = cargado;
      cargado = gltf.scene;
      oyentes.forEach((o) => o.alListo(cargado.clone()));
      // Recien aca, y no antes: los suscriptores ya cambiaron de modelo, asi
      // que nadie referencia mas las geometrias de la generacion anterior.
      if (anterior) liberar(anterior);
    },
    undefined,
    () => oyentes.forEach((o) => o.alFallar())
  );
});

/**
 * Suelta las geometrias y los materiales de un modelo que salio de escena.
 *
 * `Scene.remove()` lo saca del grafo pero no libera nada de la GPU: eso lo
 * hace `dispose()`. Con dos contextos de WebGL mirando la misma malla —el
 * visor y la vista imagen—, cada generacion dejaba su buffer en los dos, y
 * regenerar diez veces mientras se prueban medidas es lo normal en esta
 * pantalla. Los clones comparten geometria y material con la raiz, asi que
 * alcanza con liberar la raiz una vez.
 */
function liberar(raiz) {
  raiz.traverse((o) => {
    if (!o.isMesh) return;
    if (o.geometry) o.geometry.dispose();
    const materiales = Array.isArray(o.material) ? o.material : [o.material];
    materiales.forEach((m) => m && m.dispose());
  });
}

/* ── Arranque de las dos vistas ──────────────────────────────────────────── */

if (contenedor) {
  try {
    iniciar(contenedor);
  } catch (_) {
    // Sin WebGL no hay visor. El aviso se emite igual cuando llega el
    // resultado para que la pantalla ajuste el texto de las descargas: los
    // archivos ya estan habilitados, lo que falta es la vista.
    pista('pista-visor', 'este navegador no puede mostrar la vista previa 3D');
    document.addEventListener('cortante:listo', () => avisarPreview(false));
  }
}

if (lienzo) {
  try {
    iniciarImagen(lienzo);
  } catch (_) {
    // `exportarFoto` se queda en `null`, asi que el listener de arriba le
    // contesta que no se puede a quien pida la foto —el grupo de descargas
    // esconde la entrada del JPG y el ZIP avisa que va sin ella—. La vista 3D
    // y las descargas del .3mf no se enteran: son independientes de esto.
    pista('pista-imagen', 'este navegador no puede generar la imagen');
  }
}

/* ── Vista 3D ────────────────────────────────────────────────────────────── */

function iniciar(host) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = EXPOSICION_VISOR;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  host.appendChild(renderer.domElement);

  const escena = new THREE.Scene();
  escena.environment = crearEntorno(renderer);

  const camara = new THREE.PerspectiveCamera(38, 1, 0.5, 4000);
  camara.position.set(90, 78, 120);

  const controles = new OrbitControls(camara, renderer.domElement);
  controles.enableDamping = true;
  controles.dampingFactor = 0.07;
  controles.autoRotate = true;
  controles.autoRotateSpeed = 0.9;
  controles.minDistance = 20;
  controles.maxDistance = 1600;
  controles.maxPolarAngle = Math.PI * 0.495; // no dejar mirar desde abajo del piso
  controles.addEventListener('start', () => {
    controles.autoRotate = false;
  });

  agregarLuces(escena);
  const piso = agregarPiso(escena);

  let modelo = null;
  let color = colorElegido('#paleta');

  cuandoCargue(
    (raiz) => {
      if (modelo) escena.remove(modelo);
      modelo = raiz;
      modelo.rotation.x = -Math.PI / 2; // el GLB del motor viene con Z arriba
      modelo.traverse((o) => {
        if (o.isMesh) {
          o.castShadow = true;
          o.receiveShadow = true;
        }
      });
      pintarPieza(modelo, color);
      escena.add(modelo);
      encuadrar(modelo);
      controles.autoRotate = true;
      // Se avisa acá y no dentro de un `requestAnimationFrame`: rAF **no
      // corre en una pestaña de fondo**, y arrancar una generacion y cambiar
      // de pestaña es lo normal cuando tarda cinco segundos. Esperar el
      // frame dejaba el aviso colgado hasta volver a mirar la pestaña.
      avisarPreview(true);
    },
    () => {
      pista('pista-visor', 'no se pudo cargar la vista previa');
      avisarPreview(false);
    }
  );

  document.addEventListener('cortante:color', (e) => {
    color = e.detail.color;
    pintarPieza(modelo, color);
  });
  document.addEventListener('tema:cambio', () => pintarPiso(piso));
  pintarPiso(piso);

  /** Apoya el modelo en el piso, lo centra y aleja la camara lo justo. */
  function encuadrar(objeto) {
    const tamano = apoyar(objeto);

    const mayor = Math.max(tamano.x, tamano.y, tamano.z);
    const distancia = (mayor / 2 / Math.tan((camara.fov * Math.PI) / 360)) * 1.85;
    camara.position.set(distancia * 0.55, distancia * 0.5, distancia * 0.75);
    camara.near = mayor / 100;
    camara.far = mayor * 40;
    camara.updateProjectionMatrix();

    controles.target.set(0, tamano.y / 2, 0);
    controles.minDistance = mayor * 0.4;
    controles.maxDistance = mayor * 8;
    controles.update();

    piso.escala(mayor);
  }

  function medir() {
    const ancho = host.clientWidth || 1;
    const alto = host.clientHeight || 1;
    renderer.setSize(ancho, alto, false);
    camara.aspect = ancho / alto;
    camara.updateProjectionMatrix();
  }
  medir();
  new ResizeObserver(medir).observe(host);

  renderer.setAnimationLoop(() => {
    controles.update();
    renderer.render(escena, camara);
  });
}

/* ── Vista imagen: foto cenital descargable ──────────────────────────────── */

/**
 * La misma pieza que el visor, con su misma sombra, a plomo y con luz neutra.
 *
 * El aspecto se comparte por construccion y no por copia: las mismas funciones
 * (`crearEntorno`, `agregarLuces`, `agregarPiso`), la misma exposicion, el
 * mismo tone mapping, el mismo tipo de sombra y la misma direccion de luz
 * principal. Lo unico propio de esta vista es **cuanta luz sin direccion hay**
 * —`ESTUDIO_FOTO` mas `neutralizarAmbiente`—, y ninguna de las dos cosas toca
 * la luz que proyecta la sombra.
 *
 * ⚠ Por que no es literalmente el mismo estudio: lo fue durante un ciclo y no
 * servia como foto. A plomo, el panel cenital del visor cae por igual sobre el
 * plato y sobre el fondo del surco —ahi adentro no hay oclusion ambiental que
 * lo tape—, asi que el grabado del marcador quedaba en 9,5 niveles de
 * contraste sobre 255: estaba dibujado, pero no se leia, y la pieza salia 72
 * niveles por encima de su propio color. Con el reparto de esta vista son
 * 29,1. Los numeros y el metodo estan en la nota de `ESTUDIO_ORBITA`.
 *
 * Lo que NO cambio, porque es lo que hace que esto sea una foto y no el visor:
 *
 * - **Camara cenital y centrada**, como una flat-lay de producto. Se mira, no
 *   se orbita: no hay `OrbitControls` ni `setAnimationLoop`. Toma fija, se
 *   rinde de a un frame cuando algo cambia.
 * - **El cuadro es cuadrado y el preview es el JPG.** El canvas se sube a
 *   2048 px solo para exportar, con la misma camara y el mismo aspecto 1:1.
 *   Un preview panoramico con una descarga cuadrada recortaria algo que el
 *   usuario nunca vio.
 * - **El fondo es el color elegido, plano y parejo de borde a borde.** Va como
 *   `scene.background`, que es un `clearColor`: no lo toca el tone mapping ni
 *   ninguna luz, asi que el hex que sale en el JPG es exactamente el de la
 *   muestra. Lo unico que lo altera es la sombra, que es el punto.
 * - **Sin grilla.** El piso es el mismo `agregarPiso` del visor pedido con
 *   `grilla: false`: recibe la sombra y nada mas. Una cuadricula sobre un
 *   fondo liso es exactamente lo que esta foto no tiene que tener.
 * - **`Sin fondo` saca el piso, no la sombra de la pieza.** Es una muestra mas
 *   de la paleta del fondo —`COLORES_FONDO` en el router, con `piso=False`— y
 *   lo unico que hace es esconder el plano que recibe la sombra proyectada: el
 *   blanco queda parejo de borde a borde y la pieza conserva la sombra PROPIA,
 *   la del relieve y las paredes del filo, que es lo que evita que parezca un
 *   recorte pegado. Como no hay sombra que entrar en el cuadro, el encuadre le
 *   devuelve a la pieza el lugar que le reservaba.
 * - **El frustum de sombra se ajusta a la pieza.** Ver `encuadrarLuz`: es lo
 *   unico de la luz del visor que NO se copia tal cual, y el motivo esta ahi.
 */
function iniciarImagen(host) {
  // `preserveDrawingBuffer` no es opcional: `toBlob` es asincronico y sin esto
  // el buffer ya se compuso y se limpio cuando el navegador va a leerlo. El
  // JPG saldria en negro, y solo en algunos navegadores.
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  // Las cinco lineas que siguen son, literalmente, las del visor. Si alguna
  // se toca alla y no aca, las dos vistas dejan de verse igual — que es
  // exactamente lo que este ciclo vino a arreglar.
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = EXPOSICION_VISOR;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  host.appendChild(renderer.domElement);

  const escena = new THREE.Scene();
  escena.environment = crearEntorno(renderer, ESTUDIO_FOTO);
  escena.background = new THREE.Color(0x808080); // se pisa abajo con la paleta

  const camara = new THREE.PerspectiveCamera(FOV_JPG, 1, 1, 4000);
  // La camara mira a plomo, y una camara que mira a plomo no tiene vertical
  // propia: hay que decirsela o queda en un gimbal lock y sale de costado. El
  // modelo entra rotado -90 en X, y esa rotacion manda el "arriba" del arte
  // original al -Z del mundo. Ese es el arriba de la foto.
  camara.up.set(0, 0, -1);
  camara.position.set(0, 100, 0);
  camara.lookAt(0, 0, 0);

  const { cielo, principal, relleno } = agregarLuces(escena);
  neutralizarAmbiente({ cielo, relleno });

  // `ShadowMaterial` es transparente salvo donde cae la sombra: el fondo pasa
  // intacto por abajo y se oscurece solo lo que se tiene que oscurecer. Es lo
  // que deja tener sombra Y fondo liso de un color exacto al mismo tiempo — un
  // piso con material iluminado meteria un degradado que el fondo no debe
  // tener. Sin grilla: el fondo de la foto es liso.
  //
  // ⚠ **A este piso NO se le aplica `pintarPiso`, y es deliberado.** El del
  // visor sigue al tema porque el fondo se lo pone el CSS de la pagina, y una
  // sombra pensada para fondo claro se pierde sobre uno oscuro. La foto no
  // tiene tema: su fondo lo elige la paleta y el resultado es un ARCHIVO que
  // el usuario se lleva. Un JPG cuyo contenido cambiara segun si la pantalla
  // estaba en claro u oscuro seria peor que la diferencia que esto deja —en
  // tema oscuro la sombra del visor es mas densa que la de la foto—, asi que
  // se prefiere que el archivo sea siempre el mismo.
  const piso = agregarPiso(escena, { grilla: false });

  let modelo = null;
  let color = colorElegido('#paleta-pieza') || colorElegido('#paleta');
  let sinPiso = false;
  let sucio = true;
  let exportando = false;
  let listo = false;

  // La muestra del fondo trae las dos cosas: el color y si ese fondo lleva
  // piso. Se leen del mismo boton para que no haya forma de que se separen.
  const muestraFondo = muestraElegida('#paleta-fondo');
  aplicarFondo(
    muestraFondo ? muestraFondo.dataset.color : null,
    Boolean(muestraFondo) && muestraFondo.dataset.sinPiso === '1'
  );

  cuandoCargue(
    (raiz) => {
      if (modelo) escena.remove(modelo);
      modelo = raiz;
      modelo.rotation.x = -Math.PI / 2; // el GLB del motor viene con Z arriba
      modelo.traverse((o) => {
        if (o.isMesh) {
          // Las dos: `castShadow` tira la sombra sobre el fondo y
          // `receiveShadow` es la sombra EN el cortante —las paredes del filo
          // sombreandose entre si, que es lo que le da espesor al dibujo.
          o.castShadow = true;
          o.receiveShadow = true;
        }
      });
      pintarPieza(modelo, color);
      escena.add(modelo);
      encuadrar(modelo);
      listo = true;
      avisarImagen(true);
      renderizar();
    },
    () => {
      // Sin modelo no hay foto, y la pantalla tiene que DECIRLO: el grupo de
      // descargas esconde la entrada del JPG y el ZIP avisa que va sin ella.
      // Los .3mf y los .stl no se enteran — son independientes de esto.
      listo = false;
      avisarImagen(false, 'no se pudo cargar la imagen');
    }
  );

  document.addEventListener('cortante:color', (e) => {
    color = e.detail.color;
    pintarPieza(modelo, color);
    renderizar();
  });

  document.addEventListener('cortante:fondo', (e) => {
    // Entrar o salir de `Sin fondo` no es solo cambiar de color: el encuadre
    // reserva lugar para la sombra proyectada, y sin piso esa sombra no
    // existe. Por eso se vuelve a encuadrar, y solo cuando el piso cambia.
    const cambiaElPiso = Boolean(e.detail.sinPiso) !== sinPiso;
    aplicarFondo(e.detail.color, e.detail.sinPiso);
    if (cambiaElPiso && modelo) encuadrar(modelo);
    renderizar();
  });

  // Mientras la vista esta escondida el host mide 0 y no se puede ni medir ni
  // rendir. Se anota como sucia y se resuelve al volver a mostrarse.
  document.addEventListener('cortante:vista', (e) => {
    if (e.detail.vista !== 'imagen') return;
    medir();
    renderizar();
  });

  /* La otra mitad del guard contra fotografiar la pieza equivocada.
   *
   * Entre que llega un `cortante:listo` nuevo y que el `.glb` termina de
   * cargar pasan cientos de milisegundos o segundos, y en todo ese rato
   * `modelo` sigue siendo la pieza ANTERIOR. Sin esto, un pedido de
   * exportacion en esa ventana rendia y subia el cortante viejo como foto del
   * trabajo nuevo. Se baja aca y lo vuelve a subir `cuandoCargue`, que es el
   * unico que sabe que el modelo que hay corresponde al trabajo que se pidio. */
  document.addEventListener('cortante:listo', () => {
    listo = false;
  });

  // Lo que `app.js` invoca cuando el usuario pide el JPG o el ZIP. Se registra
  // ACA, con la vista viva; el listener del evento vive a nivel de modulo para
  // poder contestar que no se puede aun cuando esta funcion nunca corrio.
  exportarFoto = subir;

  function aplicarFondo(hex, fondoSinPiso) {
    if (!hex) return;
    sinPiso = Boolean(fondoSinPiso);
    // El piso es lo UNICO que recibe la sombra proyectada —es un
    // `ShadowMaterial`, invisible salvo donde cae—, asi que esconderlo deja el
    // fondo parejo de borde a borde sin tocar nada mas. La sombra propia del
    // cortante no se entera: la dibuja `receiveShadow` sobre el modelo.
    piso.sombra.visible = !sinPiso;
    escena.background.set(hex);
    // El `groundColor` del hemisferico es el rebote del fondo sobre la pieza:
    // sube con el color elegido, como pasa en una mesa de fotos real. Sin eso
    // la pieza queda pegoteada arriba del fondo en vez de apoyada en el. Es la
    // unica diferencia con el hemisferico del visor, y es la misma idea: el
    // rebote viene de lo que la pieza tiene abajo.
    cielo.groundColor.set(hex);
  }

  /** Camara a plomo sobre el centro de la pieza, y la luz a su escala. */
  function encuadrar(objeto) {
    const tamano = apoyar(objeto);
    const radio = Math.max(tamano.x, tamano.z) / 2 || 1;
    // Lo que hay que meter en el cuadro es la pieza MAS su sombra, no la pieza
    // sola: la sombra sale toda para el mismo lado y es lo primero que se
    // corta. La pieza igual queda centrada — lo que crece es el cuadro.
    //
    // El tope de `TOPE_SOMBRA` veces el radio es por el caso raro pero posible
    // de una pieza tan alta como ancha (`filo_alto_mm` admite hasta 1000): ahi
    // la sombra es casi tan grande como la pieza, y darle todo el lugar que
    // pide dejaba la pieza en la mitad del cuadro. Entre recortarle la punta a
    // la sombra y alejar el cortante, gana la sombra recortada: el sujeto de
    // la foto es la pieza, y "no demasiado lejos" fue el pedido. Con el tope,
    // la pieza nunca baja del 67% del semicuadro.
    //
    // Con `Sin fondo` no hay sombra proyectada, asi que no se reserva nada y
    // el cortante se lleva el semicuadro entero. En una pieza de 90 mm y 14 mm
    // de alto eso es un 27% mas grande: la diferencia entre una foto de
    // catalogo y una con un margen blanco que no dice nada.
    const lugarSombra = sinPiso
      ? 0
      : Math.min(tamano.y * SOMBRA_POR_MM, radio * TOPE_SOMBRA);
    const aEncuadrar = radio + lugarSombra;

    // La distancia se mide desde la CARA DE ARRIBA, no desde el piso: es la
    // que esta mas cerca del lente, asi que es la que decide si la pieza entra
    // en el cuadro. Medida desde el piso, una pieza alta se salia por los
    // bordes.
    const alto = tamano.y + (aEncuadrar / Math.tan((FOV_JPG * Math.PI) / 360)) * MARGEN_JPG;
    camara.position.set(0, alto, 0);
    camara.lookAt(0, 0, 0);
    camara.near = radio * 0.05;
    camara.far = alto * 4;
    camara.updateProjectionMatrix();

    // La direccion es la MISMA del visor (`DIR_PRINCIPAL`) — es la mitad de
    // que las dos vistas se vean iguales. Lo que cambia es la escala, y solo
    // la escala: ver la nota de `encuadrarLuz`.
    encuadrarLuz(principal, new THREE.Vector3(...DIR_PRINCIPAL), radio);

    piso.escala(radio * 3.5);
  }

  /**
   * Lo unico de la luz del visor que NO se copia tal cual — y hace falta.
   *
   * `agregarLuces` deja la luz en una posicion fija y con un frustum de sombra
   * fijo de ±160. Alla esta bien: la pieza se ve entera y de lejos, y 160 la
   * cubre. **Aca no**, por dos motivos que van juntos:
   *
   * 1. `lado_mayor_mm` admite hasta 1000, asi que una pieza de mas de 320 mm
   *    se sale del frustum y **se queda sin sombra** — desaparecida, no mas
   *    chica.
   * 2. Aunque entrara, repartir 2048 texels sobre un frustum de mas del doble
   *    de la pieza vuelve la sombra un escalon en vez de un borde, y en una
   *    exportacion de 2048 px eso se ve.
   *
   * Ajustarlo a la pieza es invisible en el resultado: el frustum no cambia el
   * aspecto, solo la resolucion con que se calcula. Las dos vistas siguen
   * viendose iguales.
   */
  function encuadrarLuz(luz, direccion, radio) {
    luz.position.copy(direccion).normalize().multiplyScalar(radio * 9);
    const c = luz.shadow.camera;
    c.left = -radio * 2.2;
    c.right = radio * 2.2;
    c.top = radio * 2.2;
    c.bottom = -radio * 2.2;
    // ⚠ El rango de profundidad va HOLGADO, y no es descuido. Ceñirlo es el
    // consejo habitual para VSM —mas precision, menos luz filtrada— pero aca
    // hace exactamente lo contrario: el piso se extiende 7 radios y con la luz
    // en diagonal hay puntos de piso MAS CERCA de la luz que la pieza, asi que
    // un `near` ajustado a la pieza los recorta y el fondo se llena de bandas
    // diagonales. Medido: con `near` en 5 radios el desvio del fondo pasa de
    // 0,77 a 5,94 niveles. Holgado y parejo le gana a ceñido y rayado.
    c.near = radio;
    c.far = radio * 20;
    c.updateProjectionMatrix();
  }

  function medir() {
    const ancho = host.clientWidth;
    const alto = host.clientHeight;
    if (!ancho || !alto) {
      sucio = true;
      return false;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(ancho, alto, false);
    camara.aspect = ancho / alto;
    camara.updateProjectionMatrix();
    return true;
  }

  /** Un frame, ahora. No hay loop: la toma es fija y solo cambia si se toca. */
  function renderizar() {
    if (exportando) return;
    if (!host.clientWidth || !host.clientHeight) {
      sucio = true;
      return;
    }
    if (sucio) {
      if (!medir()) return;
      sucio = false;
    }
    renderer.render(escena, camara);
  }

  medir();
  new ResizeObserver(() => {
    sucio = true;
    renderizar();
  }).observe(host);

  /**
   * Rinde la foto y la sube al trabajo. **Se corre antes de cada descarga.**
   *
   * Es lo que sostiene que lo que baja sea lo que se esta viendo: la foto
   * depende del color de la pieza y del fondo, que el usuario puede cambiar en
   * cualquier momento. Subir al renderizar —con o sin espera— abriria una
   * ventana en la que el servidor tiene una foto vieja y nadie se entera, que
   * es exactamente el fallback silencioso que este proyecto no se permite.
   *
   * El costo es un render de 2048 px y una subida por descarga. Se paga: es un
   * clic explicito del usuario, no una tecla de la paleta.
   *
   * ⚠ **El destino lo manda quien pide la foto, no lo arma este modulo.** Antes
   * se recibia el id del trabajo y la URL se escribia aca; con F4 hay dos
   * destinos posibles —`/imagen` para un cortante y `/diseno/<n>/imagen` para
   * cada diseño de un post— y elegir entre ellos desde el visor seria meterle
   * al visor una idea de que pantalla lo llamo. El visor rinde; quien pidio la
   * foto sabe donde va.
   */
  async function subir(destino) {
    // Contesta hasta para decir que no puede: es la unica salida de esta
    // funcion que antes se iba muda, y quien la llama espera una respuesta
    // sin la cual se queda esperando para siempre.
    if (exportando) {
      avisarImagen(false, 'ya hay una exportacion en curso', 'exportar');
      return;
    }
    if (!destino) {
      avisarImagen(false, 'no se sabe donde subir la imagen', 'exportar');
      return;
    }
    if (!modelo || !listo) {
      avisarImagen(false, 'todavia no hay imagen para exportar', 'exportar');
      return;
    }
    exportando = true;
    pista('pista-imagen', 'armando el JPG…');
    try {
      const blob = await aJpg();
      if (!blob) {
        avisarImagen(false, 'no se pudo generar el JPG', 'exportar');
        return;
      }
      const cuerpo = new FormData();
      // El nombre es de relleno: el servidor guarda por `NOMBRE_DE` y este
      // valor no toca ninguna ruta. Va uno fijo justamente para dejar claro
      // que el cliente no nombra nada.
      cuerpo.append('archivo', blob, 'vista.jpg');
      const r = await fetch(destino, { method: 'PUT', body: cuerpo });
      if (!r.ok) {
        avisarImagen(false, 'el servidor rechazo la imagen', 'exportar');
        return;
      }
      const mb = (blob.size / 1024 / 1024).toFixed(2);
      pista('pista-imagen', `JPG listo · ${mb} MB`);
      avisarImagen(true, null, 'exportar');
    } catch (_) {
      // Rendir a 2048 px es lo mas caro que hace esta pantalla y es donde se
      // puede perder el contexto de WebGL; la subida puede fallar por red.
      // Sin este `catch` la excepcion queda como rechazo sin atender y —lo
      // peor— quien espera el `cortante:imagen` no recibe nunca su respuesta.
      avisarImagen(false, 'no se pudo generar el JPG', 'exportar');
    } finally {
      exportando = false;
      sucio = true;
      renderizar();
    }
  }

  /**
   * Rinde a tamaño de exportacion y devuelve el primer JPG que entra en 2 MB.
   *
   * El aspecto sigue siendo 1:1 y la camara no se mueve, asi que el encuadre
   * del archivo es identico al del preview — solo con mas pixeles. Cada
   * `toBlob` vuelve a comprimir el MISMO buffer, que es el motivo por el que
   * el renderer se creo con `preserveDrawingBuffer`.
   */
  async function aJpg() {
    let mejor = null;
    try {
      for (const lado of LADOS_JPG) {
        renderer.setPixelRatio(1);
        renderer.setSize(lado, lado, false);
        camara.aspect = 1;
        camara.updateProjectionMatrix();
        renderer.render(escena, camara);

        for (const calidad of CALIDADES_JPG) {
          const blob = await new Promise((r) =>
            renderer.domElement.toBlob(r, 'image/jpeg', calidad)
          );
          if (!blob) continue;
          if (blob.size <= TOPE_BYTES) return blob;
          if (!mejor || blob.size < mejor.size) mejor = blob;
        }
      }
      return mejor;
    } finally {
      sucio = true;
    }
  }
}

/* ── Entorno, luces y piso ───────────────────────────────────────────────── */

/** Caja de paneles emisivos convertida a mapa de entorno. Sin archivos. */
function crearEntorno(renderer, reparto = ESTUDIO_ORBITA) {
  const pmrem = new THREE.PMREMGenerator(renderer);
  const cuarto = new THREE.Scene();
  cuarto.background = new THREE.Color(0x1c2126);

  const panel = (color, intensidad, escala, pos, rot) => {
    const malla = new THREE.Mesh(
      new THREE.PlaneGeometry(escala[0], escala[1]),
      new THREE.MeshBasicMaterial({ color, side: THREE.DoubleSide })
    );
    malla.material.color.multiplyScalar(intensidad);
    malla.position.set(...pos);
    malla.rotation.set(...rot);
    cuarto.add(malla);
  };

  panel(0xffffff, reparto.cenital, [12, 8], [0, 7, 0], [Math.PI / 2, 0, 0]); // cenital
  panel(reparto.frio, reparto.lateral, [10, 8], [-7, 2, 2], [0, Math.PI / 2, 0]); // lateral
  panel(reparto.calido, reparto.contra, [10, 8], [7, 2, -2], [0, -Math.PI / 2, 0]); // contra
  panel(0x2a3138, reparto.piso, [16, 16], [0, -3, 0], [-Math.PI / 2, 0, 0]); // piso

  const objetivo = pmrem.fromScene(cuarto, 0.04);
  pmrem.dispose();
  return objetivo.texture;
}

/**
 * Las luces del visor. **Devuelve las que hacen falta ajustar despues.**
 *
 * La vista 3D las usa tal cual y descarta el retorno. La vista imagen se queda
 * con las tres: `principal` para reajustarle el frustum a la pieza (ver la
 * nota de `encuadrarLuz`), `cielo` tanto para que el rebote siga al color del
 * fondo como para bajarlo, y `relleno` para neutralizarlo — las dos ultimas
 * via `neutralizarAmbiente`. Ver tambien la nota de `encuadrarLuz` sobre por
 * que el frustum fijo de aca abajo no sirve para exportar.
 */
function agregarLuces(escena) {
  const cielo = new THREE.HemisphereLight(0xffffff, 0x9fb4c0, 0.55);
  escena.add(cielo);

  const principal = new THREE.DirectionalLight(0xffffff, 2.1);
  principal.position.set(...DIR_PRINCIPAL);
  principal.castShadow = true;
  principal.shadow.mapSize.set(2048, 2048);
  principal.shadow.bias = -0.0006;
  principal.shadow.normalBias = 0.4;
  const c = principal.shadow.camera;
  c.left = -160;
  c.right = 160;
  c.top = 160;
  c.bottom = -160;
  c.far = 600;
  escena.add(principal);

  const relleno = new THREE.DirectionalLight(0xdcf0fb, 0.7);
  relleno.position.set(-90, 50, -70);
  escena.add(relleno);

  return { cielo, principal, relleno };
}

/**
 * Deja el ambiente de la foto neutro y mas bajo. **No toca `principal`.**
 *
 * Que la luz principal no aparezca aca es la mitad del punto: es la que
 * proyecta la sombra, y la sombra tenia que quedar igual. Conserva la
 * direccion, el color y la intensidad del visor, y la mancha sobre el fondo la
 * pinta `ShadowMaterial` con una opacidad fija que no depende de ninguna luz,
 * asi que ni siquiera indirectamente cambia. Lo unico que se mueve es cuanta
 * luz sin direccion hay rellenando el grabado.
 *
 * El relleno del visor es celeste (`0xdcf0fb`) para enfriar la cara en sombra
 * mientras la pieza gira. A plomo eso llega como un tinte azul sobre las
 * paredes del grabado —medido sobre un gris neutro: 4 niveles de rojo abajo y
 * 3 de azul arriba—. En blanco queda en cero, que es lo que se pidio.
 */
function neutralizarAmbiente({ cielo, relleno }) {
  cielo.intensity = AMBIENTE_FOTO.hemisferico;
  relleno.color.set(0xffffff);
  relleno.intensity = AMBIENTE_FOTO.relleno;
}

/**
 * El piso: un plano que solo recibe sombra, y opcionalmente la grilla.
 *
 * `grilla` es una opcion y no dos funciones distintas para que el piso siga
 * teniendo **un solo dueño**: la vista 3D la quiere —es la referencia de
 * escala mientras la pieza gira— y la foto no, porque el pedido es un fondo
 * liso. Que la diferencia sea un booleano explicito y no un piso paralelo es
 * lo que evita que las dos vistas se separen sin que nadie se entere.
 */
function agregarPiso(escena, { grilla: conGrilla = true } = {}) {
  const sombra = new THREE.Mesh(
    new THREE.PlaneGeometry(1, 1),
    new THREE.ShadowMaterial({ opacity: 0.24 })
  );
  sombra.rotation.x = -Math.PI / 2;
  sombra.position.y = -0.01;
  sombra.receiveShadow = true;
  escena.add(sombra);

  let grilla = null;

  return {
    sombra,
    escala(mayor) {
      const lado = Math.max(mayor * 4, 200);
      sombra.scale.set(lado, lado, 1);
      if (!conGrilla) return;
      if (grilla) {
        escena.remove(grilla);
        // `remove` lo saca del grafo pero no libera nada de la GPU. Regenerar
        // diez veces mientras se prueban medidas es lo normal en esta
        // pantalla, asi que sin esto queda una grilla por generacion.
        grilla.geometry.dispose();
        grilla.material.dispose();
      }
      grilla = new THREE.GridHelper(lado, Math.round(lado / 10), 0x8fa8b6, 0x8fa8b6);
      grilla.material.transparent = true;
      grilla.material.opacity = 0.16;
      grilla.position.y = -0.005;
      escena.add(grilla);
    },
    get grilla() {
      return grilla;
    },
  };
}

/** El piso sigue al tema: el fondo lo pone el CSS, la grilla se ajusta. */
function pintarPiso(piso) {
  const oscuro =
    document.documentElement.dataset.tema === 'oscuro' ||
    (!document.documentElement.dataset.tema &&
      window.matchMedia('(prefers-color-scheme: dark)').matches);
  piso.sombra.material.opacity = oscuro ? 0.4 : 0.24;
  if (piso.grilla) {
    piso.grilla.material.opacity = oscuro ? 0.1 : 0.16;
    piso.grilla.material.color.set(oscuro ? 0x4fc6ee : 0x8fa8b6);
  }
}
