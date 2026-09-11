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

/* Direccion de la luz clave, y de ahi el largo de la sombra.
 *
 * Se declara una sola vez porque **el encuadre depende de ella**: la sombra se
 * corre `SOMBRA_POR_MM` milimetros por cada milimetro de alto de la pieza, en
 * el eje x y en el z, y si el cuadro no la contempla sale cortada. Tenerlas
 * separadas —un vector para la luz y un margen a ojo para la camara— es
 * exactamente como se cortaba antes: con 58 grados de elevacion la sombra de
 * una pieza de 14 mm se corre 6,1 mm, y el aire que dejaba el margen fijo eran
 * 6,0. Entraba por casualidad, y con una pieza mas alta no entraba.
 *
 * 1,6 sobre la hipotenusa de 0,7 y 0,7 son 58 grados. Mas alta lava el
 * relieve del marcador; mas baja alarga tanto la sombra que la pieza tiene que
 * achicarse para que entre.
 */
const DIR_CLAVE = [-0.7, 1.6, -0.7];
const SOMBRA_POR_MM = Math.abs(DIR_CLAVE[0]) / DIR_CLAVE[1];

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

/* Intensidad de cada panel de la caja de estudio, por vista.
 *
 * El visor 3D usa `ORBITA`, que es la caja original: cenital fuerte, porque
 * ahi la pieza gira y lo que se busca es que se lea el volumen desde cualquier
 * angulo.
 *
 * `FOTO.estudio` es la de la toma a plomo, y **casi invierte el reparto**. El
 * motivo es geometrico: en una toma cenital el panel de arriba ilumina por
 * igual la cara del plato y la cara del relieve —las dos son horizontales y
 * miran al mismo lado—, asi que todo lo que le sobra es lavado que borra el
 * marcador. La luz lateral, en cambio, separa las PAREDES del relieve del
 * plato, que es lo unico que hace que un grabado se lea. Y sale gratis en
 * sombras: el `environment` no proyecta ninguna.
 *
 * ⚠ Vive ACA ARRIBA y no al lado de `crearEntorno`, que seria su lugar
 * natural: `crearEntorno` la toma como valor por defecto de un parametro, y
 * las dos vistas se inicializan mas arriba que el final del archivo. Un
 * `const` declarado despues esta en zona muerta temporal, asi que la llamada
 * tiraba `ReferenceError` — y lo peor es que no se veia: el `try` del arranque
 * se lo comia y la pantalla decia "este navegador no puede generar la imagen".
 * Una funcion se hubiera hoisteado; un `const` no.
 */
const ESTUDIO_ORBITA = { cenital: 3.2, lateral: 1.5, contra: 0.9, piso: 1.0 };

/* Los numeros de la toma cenital, juntos.
 *
 * Estan agrupados y no repartidos por el codigo porque **se ajustan juntos**:
 * subir la clave sin bajar la exposicion vuelve a quemar el plato del
 * marcador, y bajar el entorno sin subir la clave apaga la pieza entera. Lo
 * que se busca con el reparto es que la mayor parte de la luz venga de una
 * direccion —la clave— y no del ambiente, porque el ambiente rellena
 * justamente las sombras del relieve que hay que ver.
 */
const FOTO = {
  exposicion: 0.82,
  clave: 3.4, // luz principal, 58 grados de elevacion
  cenital: 0.26, // relleno casi a plomo; el que pone el nucleo del contacto
  hemisferico: 0.14, // rebote del fondo sobre la pieza
  sombraPiso: 0.85, // opacidad del ShadowMaterial del piso
  biasNormal: 0.02, // en mm — ver la nota de `iniciarImagen`
  estudio: { cenital: 0.3, lateral: 1.8, contra: 0.6, piso: 0.35 },

  /* Difusion de cada sombra: cuanto se abre su penumbra (`shadow.radius`).
   *
   * Es lo que convierte una sombra de silueta recortada en una de fuente
   * grande. Escala con el mapa y no con milimetros, y el frustum de sombra
   * escala con la pieza, asi que la penumbra sale **proporcional** al tamaño
   * del cortante: la foto se ve igual con uno de 40 mm que con uno de 200.
   *
   * Las dos difusiones son distintas y ese es el punto: la clave va muy
   * abierta y arma la penumbra ancha; la cenital va mas cerrada y arma el
   * apoyo pegado al contacto. Una sola sombra no puede ser las dos cosas.
   *
   * Medido contra la foto de referencia (32% de caida, 18 niveles por 1% del
   * ancho): con 45 la sombra cae 29% con 9 niveles — la misma presencia que la
   * foto y el doble de difuminada, que es lo que se pidio. Antes de esto caia
   * 34% de golpe, en un escalon de 50 niveles.
   */
  mapaSombra: 2048, // lado del shadow map de cada luz
  difusionClave: 45,
  difusionCenital: 11,

  /* Aporte de cada sombra al oscurecimiento (`getShadowMask` los multiplica).
   *
   * Bajos y distintos para que la sombra **no sea pareja**: donde llega solo
   * la clave el fondo baja ~10%, y donde se superponen las dos ~19%. Asi la
   * sombra es apenas un velo lejos de la pieza y se cierra contra el apoyo,
   * que es como cae una sombra de verdad. Con un solo valor alto quedaba una
   * mancha de densidad uniforme, que es justo lo que se veia "marcado".
   */
  aporteClave: 0.45,
  aporteCenital: 0.65,
};

/* ── Ganchos de la pantalla ──────────────────────────────────────────────── */

const contenedor = document.getElementById('visor');
const lienzo = document.getElementById('lienzo');

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
  const muestra = document.querySelector(`${selector} .paleta__color[aria-pressed="true"]`);
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
    // El boton de descarga nace apagado en el HTML, asi que alcanza con
    // explicar por que se queda asi. La vista 3D y las descargas del .3mf no
    // se enteran: son independientes de esto.
    pista('pista-imagen', 'este navegador no puede generar la imagen');
  }
}

/* ── Vista 3D ────────────────────────────────────────────────────────────── */

function iniciar(host) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
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
 * La misma pieza, fotografiada a plomo sobre un fondo liso.
 *
 * Existe para lo que antes obligaba a imprimir, sacar una foto, editarla y
 * recien entonces publicarla. Por eso las decisiones no son las del visor:
 *
 * - **Camara cenital y centrada**, como una foto flat-lay de producto. Se
 *   mira, no se orbita: no hay `OrbitControls` ni `setAnimationLoop`. Es una
 *   toma fija y se rinde de a un frame, cuando algo cambia.
 * - **El cuadro es cuadrado y el preview es el JPG.** El canvas se sube a
 *   2048 px solo para exportar, con la misma camara y el mismo aspecto 1:1.
 *   Un preview panoramico con una descarga cuadrada recortaria algo que el
 *   usuario nunca vio.
 * - **El fondo es el color elegido, plano y parejo de borde a borde.** Va como
 *   `scene.background`, que es un `clearColor`: no lo toca el tone mapping ni
 *   ninguna luz, asi que el hex que sale en el JPG es exactamente el de la
 *   muestra. La unica cosa que lo altera es la sombra, que es el punto.
 * - **Dos sombras, no una.** Una luz clave a 70 grados tira la sombra corrida
 *   hacia un lado —con la camara a plomo, la de abajo de la pieza la tapa la
 *   pieza, asi que sin esa inclinacion no se veria ninguna— y una cenital casi
 *   a plomo agrega el nucleo oscuro pegado al contacto. Las dos con
 *   `shadow.intensity` parcial: `getShadowMask()` multiplica el aporte de cada
 *   luz, asi que donde se superponen queda mas oscuro. Eso es lo que separa una
 *   pieza apoyada de una pieza pegada, y es todo el realismo de la toma.
 */
function iniciarImagen(host) {
  // `preserveDrawingBuffer` no es opcional: `toBlob` es asincronico y sin esto
  // el buffer ya se compuso y se limpio cuando el navegador va a leerlo. El
  // JPG saldria en negro, y solo en algunos navegadores.
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  // Mas baja que la del visor (1,05), y no por gusto: con la pieza llenando el
  // cuadro y el plato del marcador de frente al lente, la exposicion del visor
  // dejaba las caras de arriba en 240 sobre un maximo de 243 — todo el
  // marcador apretado en el 2% mas alto del rango, donde ningun sombreado
  // tiene lugar para verse. Medido en la region del marcador: de 241,7 de
  // media a 219, y el detalle local casi al doble.
  renderer.toneMappingExposure = FOTO.exposicion;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.shadowMap.enabled = true;
  /* `PCFShadowMap` —el del medio— y no el `PCFSoftShadowMap` del visor.
   *
   * Suena al reves y no lo es: **`PCFSoftShadowMap` ignora `shadow.radius`**.
   * Filtra con un kernel fijo de pocos texels, asi que su borde es suave dos o
   * tres pixeles y despues es un escalon. Medido contra la foto de referencia,
   * su transicion daba 1,3% del ancho de imagen contra el 5,8% de la foto, y
   * encima de densidad pareja: eso es lo que se leia como "sombra marcada".
   * `PCFShadowMap` **si** escala su muestreo con `shadow.radius`, y con eso la
   * penumbra se abre todo lo que haga falta.
   *
   * Tambien se probo `VSMShadowMap`, que desenfoca el mapa de verdad y da la
   * penumbra mas pareja de las tres. Se descarto por el FONDO: filtra luz
   * donde no hay nada que sombree y lo deja con bandas diagonales de 4 o 5
   * niveles (desvio 0,77 contra 0,00 de PCF). Con un fondo liso de un color
   * elegido por el usuario eso se ve, y el pedido era que el fondo sea liso.
   * Ceñir el rango de profundidad —el consejo habitual para VSM— lo empeora
   * aca, por el motivo que explica `encuadrarLuz`. */
  renderer.shadowMap.type = THREE.PCFShadowMap;
  host.appendChild(renderer.domElement);

  const escena = new THREE.Scene();
  escena.environment = crearEntorno(renderer, FOTO.estudio);
  escena.background = new THREE.Color(0x808080); // se pisa abajo con la paleta

  const camara = new THREE.PerspectiveCamera(FOV_JPG, 1, 1, 4000);
  // La camara mira a plomo, y una camara que mira a plomo no tiene vertical
  // propia: hay que decirsela o queda en un gimbal lock y sale de costado. El
  // modelo entra rotado -90 en X, y esa rotacion manda el "arriba" del arte
  // original al -Z del mundo. Ese es el arriba de la foto.
  camara.up.set(0, 0, -1);
  camara.position.set(0, 100, 0);
  camara.lookAt(0, 0, 0);

  // El `groundColor` del hemisferico es el rebote del fondo sobre la pieza:
  // sube con el color del fondo, como pasa en una mesa de fotos real. Sin eso
  // la pieza queda pegoteada arriba del fondo en vez de apoyada en el.
  //
  // Va bajo por lo mismo que el panel cenital del entorno: un hemisferico
  // ilumina el plato y el relieve casi igual, asi que de mas solo aporta
  // lavado. Lo que hace falta de el es el rebote de color, no exposicion.
  const cielo = new THREE.HemisphereLight(0xffffff, 0x808080, FOTO.hemisferico);
  escena.add(cielo);

  /* ⚠ `normalBias` es el ajuste que decide si el marcador se ve o no.
   *
   * Estaba en 0,35 — **milimetros**, copiado del visor, donde la pieza se ve
   * de lejos y entera. Sobre un relieve de 2 mm eso es un corrimiento del 17%
   * de la altura del trazo: la consulta al shadow map se va tan afuera de la
   * superficie que **borra justo las sombras propias del relieve**, que son
   * las que dibujan el grabado. Medido: el plato y la cara del trazo salian
   * los dos en 240 y entre ellos no habia mas que una linea de un pixel.
   *
   * Con 0,02 mm las sombras del relieve vuelven a existir. El precio posible
   * es acne de sombra en las caras planas, y lo que lo mantiene a raya es que
   * el frustum es chico: `radio * 2.2` sobre 2048 texels son ~0,05 mm por
   * texel, dos veces mas fino que el bias que se saco.
   */

  // La luz clave se lleva la mayor parte del presupuesto, y a 58 grados en vez
  // de los 70 de antes. Las dos cosas van juntas y apuntan a lo mismo: la
  // sombra propia del relieve pasa de 0,36 a 0,62 veces la altura del trazo
  // —de una linea a una banda que se lee—, y que la luz venga de UNA direccion
  // en vez del ambiente es lo que evita que esa sombra se rellene sola. Es el
  // movimiento que hace un fotografo para que un sello se vea: bajar la luz y
  // apagar el relleno.
  const clave = new THREE.DirectionalLight(0xffffff, FOTO.clave);
  ajustarSombra(clave, FOTO.difusionClave, FOTO.aporteClave);
  escena.add(clave);

  const cenital = new THREE.DirectionalLight(0xfaf6ef, FOTO.cenital);
  ajustarSombra(cenital, FOTO.difusionCenital, FOTO.aporteCenital);
  escena.add(cenital);

  // `ShadowMaterial` es transparente salvo donde cae la sombra: el fondo pasa
  // intacto por abajo y se oscurece solo lo que se tiene que oscurecer. Es lo
  // que deja tener sombra Y fondo liso de un color exacto al mismo tiempo — un
  // piso con material iluminado meteria un degradado que el fondo no debe
  // tener.
  const piso = new THREE.Mesh(
    new THREE.PlaneGeometry(1, 1),
    new THREE.ShadowMaterial({ opacity: FOTO.sombraPiso })
  );
  piso.rotation.x = -Math.PI / 2;
  piso.receiveShadow = true;
  escena.add(piso);

  const boton = document.getElementById('bajar-imagen');
  let modelo = null;
  let color = colorElegido('#paleta-pieza') || colorElegido('#paleta');
  let sucio = true;
  let exportando = false;

  aplicarFondo(colorElegido('#paleta-fondo'));

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
      if (boton) {
        boton.disabled = false;
        boton.removeAttribute('title');
      }
      renderizar();
    },
    () => {
      pista('pista-imagen', 'no se pudo cargar la imagen');
    }
  );

  document.addEventListener('cortante:color', (e) => {
    color = e.detail.color;
    pintarPieza(modelo, color);
    renderizar();
  });

  document.addEventListener('cortante:fondo', (e) => {
    aplicarFondo(e.detail.color);
    renderizar();
  });

  // Mientras la vista esta escondida el host mide 0 y no se puede ni medir ni
  // rendir. Se anota como sucia y se resuelve al volver a mostrarse.
  document.addEventListener('cortante:vista', (e) => {
    if (e.detail.vista !== 'imagen') return;
    medir();
    renderizar();
  });

  if (boton) boton.addEventListener('click', descargar);

  function aplicarFondo(hex) {
    if (!hex) return;
    escena.background.set(hex);
    cielo.groundColor.set(hex);
  }

  /** Camara a plomo sobre el centro de la pieza, y las dos luces a su escala. */
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
    const aEncuadrar = radio + Math.min(tamano.y * SOMBRA_POR_MM, radio * TOPE_SOMBRA);

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

    // Arriba-izquierda del cuadro: con `camara.up` en -Z, el +X del mundo es
    // la derecha de la foto y el -Z es el arriba. La sombra sale entonces
    // hacia abajo-derecha, y cuanto se corre lo dice `SOMBRA_POR_MM`, que es
    // el mismo numero que uso el encuadre de arriba.
    encuadrarLuz(clave, new THREE.Vector3(...DIR_CLAVE), radio);
    // Casi a plomo: su sombra apenas sobresale del contorno y es la que pone
    // el nucleo oscuro del contacto.
    encuadrarLuz(cenital, new THREE.Vector3(0.22, 6, 0.3), radio);

    piso.scale.set(radio * 14, radio * 14, 1);
    // Apenas por debajo del apoyo de la pieza: coplanar con la cara de abajo
    // el shadow map alterna entre los dos y la sombra sale moteada.
    piso.position.y = -radio * 0.004;
  }

  /** Deja una luz lista para tirar sombra difusa: resolucion, blur y aporte. */
  function ajustarSombra(luz, difusion, aporte) {
    luz.castShadow = true;
    luz.shadow.mapSize.set(FOTO.mapaSombra, FOTO.mapaSombra);
    // En 0 y no negativo: con el muestreo abierto de `radius`, un bias
    // negativo corre la comparacion lo suficiente como para dejar un halo
    // claro alrededor del contacto.
    luz.shadow.bias = 0;
    luz.shadow.normalBias = FOTO.biasNormal;
    luz.shadow.radius = difusion;
    luz.shadow.intensity = aporte;
  }

  function encuadrarLuz(luz, direccion, radio) {
    luz.position.copy(direccion).normalize().multiplyScalar(radio * 9);
    const c = luz.shadow.camera;
    // Ajustado a la pieza y no a la escena: el shadow map tiene 2048 px y
    // repartirlos sobre un frustum de mas del doble de la pieza es lo que
    // vuelve la sombra un escalon en vez de un borde.
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

  async function descargar() {
    if (exportando || !modelo) return;
    exportando = true;
    if (boton) boton.disabled = true;
    pista('pista-imagen', 'armando el JPG…');
    try {
      const blob = await aJpg();
      if (!blob) {
        pista('pista-imagen', 'no se pudo generar el JPG');
        return;
      }
      bajarBlob(blob, nombreDescarga());
      const mb = (blob.size / 1024 / 1024).toFixed(2);
      pista('pista-imagen', `JPG descargado · ${mb} MB`);
    } catch (_) {
      // Rendir a 2048 px es lo mas caro que hace esta pantalla y es donde se
      // puede perder el contexto de WebGL. Sin este `catch` la excepcion se
      // escapa del handler del click, queda como rechazo sin atender y —lo
      // peor— la pista se queda en "armando el JPG…" para siempre: el boton
      // vuelve a estar vivo pero el cartel dice que no.
      pista('pista-imagen', 'no se pudo generar el JPG');
    } finally {
      exportando = false;
      if (boton) boton.disabled = false;
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

/**
 * El JPG se llama como el archivo que el usuario subio.
 *
 * Se lee del chip de la pantalla y no del servidor porque este archivo no pasa
 * por el servidor: sale del canvas. Se sanea con la misma whitelist que usa
 * `sanear_nombre_base` en `app/archivos.py` —`[A-Za-z0-9._-]`, tope 60— para
 * que la descarga se vea igual que las del `.3mf`.
 */
function nombreDescarga() {
  const chip = document.getElementById('nombre-archivo');
  const base = ((chip && chip.textContent) || '')
    .replace(/\.[^.]*$/, '')
    .replace(/[^A-Za-z0-9._-]+/g, '_')
    .replace(/^[._-]+/, '')
    .slice(0, 60);
  return `${base || 'cortante'}-vista.jpg`;
}

function bajarBlob(blob, nombre) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = nombre;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revocar en el mismo turno le corta la descarga a algunos navegadores: se
  // libera despues, cuando ya la agarro.
  setTimeout(() => URL.revokeObjectURL(url), 30000);
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
  panel(0xd8ecf7, reparto.lateral, [10, 8], [-7, 2, 2], [0, Math.PI / 2, 0]); // relleno frio
  panel(0xfff0dd, reparto.contra, [10, 8], [7, 2, -2], [0, -Math.PI / 2, 0]); // contra calido
  panel(0x2a3138, reparto.piso, [16, 16], [0, -3, 0], [-Math.PI / 2, 0, 0]); // piso

  const objetivo = pmrem.fromScene(cuarto, 0.04);
  pmrem.dispose();
  return objetivo.texture;
}

function agregarLuces(escena) {
  escena.add(new THREE.HemisphereLight(0xffffff, 0x9fb4c0, 0.55));

  const principal = new THREE.DirectionalLight(0xffffff, 2.1);
  principal.position.set(70, 130, 90);
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
}

function agregarPiso(escena) {
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
      if (grilla) escena.remove(grilla);
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
