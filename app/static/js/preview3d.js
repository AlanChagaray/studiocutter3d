/* Vista previa 3D del cortante.
 *
 * Carga el `.glb` que produjo el mismo pipeline que el `.3mf`: no es una
 * aproximacion ni una reconstruccion, es **la misma geometria** exportada en
 * otro formato. Lo que se ve girando es lo que se descarga.
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
 * transformacion), asi que el modelo se rota -90 grados en X al cargarlo.
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

const contenedor = document.getElementById('visor');

/**
 * El color inicial sale de la propia paleta de la pantalla.
 *
 * No hay una segunda lista de colores aca: la unica esta en `COLORES` del
 * router, el template la dibuja y este modulo lee la muestra marcada. Si la
 * paleta no existe devuelve `null` y la pieza se queda con los materiales
 * del motor, que es la degradacion correcta.
 */
function colorElegido() {
  const muestra = document.querySelector('#paleta .paleta__color[aria-pressed="true"]');
  return muestra ? muestra.dataset.color : null;
}

if (contenedor) {
  try {
    iniciar(contenedor);
  } catch (_) {
    // Sin WebGL no hay visor. El aviso se emite igual cuando llega el
    // resultado para que la pantalla ajuste el texto de las descargas: los
    // archivos ya estan habilitados, lo que falta es la vista.
    const pista = document.getElementById('pista-visor');
    if (pista) pista.textContent = 'este navegador no puede mostrar la vista previa 3D';
    document.addEventListener('cortante:listo', () =>
      document.dispatchEvent(new CustomEvent('cortante:preview', { detail: { ok: false } }))
    );
  }
}

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
  let color = colorElegido();

  document.addEventListener('cortante:listo', (e) => {
    cargar(e.detail.url);
  });
  document.addEventListener('cortante:color', (e) => {
    color = e.detail.color;
    pintarPieza();
  });
  document.addEventListener('tema:cambio', () => pintarPiso(piso));
  pintarPiso(piso);

  /**
   * Pinta la pieza entera del color elegido, sin tocar el acabado.
   *
   * Los dos cuerpos van del mismo color a proposito: es una pieza impresa en
   * un filamento. Dejarlos de colores distintos —como vienen del motor—
   * sugiere un bicolor que la impresora no hace sola.
   */
  function pintarPieza() {
    if (!modelo || !color) return;
    modelo.traverse((o) => {
      if (!o.isMesh) return;
      const materiales = Array.isArray(o.material) ? o.material : [o.material];
      materiales.forEach((m) => {
        if (m && m.color) m.color.set(color);
      });
    });
  }

  /**
   * Avisa como termino la carga.
   *
   * La pantalla lo usa para el texto que acompaña a las descargas: si la pieza
   * se ve, dice que eso es exactamente lo que se baja. Se emite tambien cuando
   * falla, con `ok: false`. Las descargas NO dependen de esto —se habilitan al
   * terminar el trabajo—: un visor roto no dice nada del archivo.
   */
  function avisarPreview(ok) {
    document.dispatchEvent(new CustomEvent('cortante:preview', { detail: { ok } }));
  }

  function cargar(url) {
    new GLTFLoader().load(
      url,
      (gltf) => {
        if (modelo) escena.remove(modelo);
        modelo = gltf.scene;
        modelo.rotation.x = -Math.PI / 2; // el GLB del motor viene con Z arriba
        modelo.traverse((o) => {
          if (o.isMesh) {
            o.castShadow = true;
            o.receiveShadow = true;
          }
        });
        pintarPieza();
        escena.add(modelo);
        encuadrar(modelo);
        controles.autoRotate = true;
        // Se avisa acá y no dentro de un `requestAnimationFrame`: rAF **no
        // corre en una pestaña de fondo**, y arrancar una generacion y cambiar
        // de pestaña es lo normal cuando tarda cinco segundos. Esperar el
        // frame dejaba el aviso colgado hasta volver a mirar la pestaña.
        avisarPreview(true);
      },
      undefined,
      () => {
        const pista = document.getElementById('pista-visor');
        if (pista) pista.textContent = 'no se pudo cargar la vista previa';
        avisarPreview(false);
      }
    );
  }

  /** Apoya el modelo en el piso, lo centra y aleja la camara lo justo. */
  function encuadrar(objeto) {
    const caja = new THREE.Box3().setFromObject(objeto);
    const tamano = caja.getSize(new THREE.Vector3());
    const centro = caja.getCenter(new THREE.Vector3());

    objeto.position.x -= centro.x;
    objeto.position.z -= centro.z;
    objeto.position.y -= caja.min.y;

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

/* ── Entorno, luces y piso ───────────────────────────────────────────────── */

/** Caja de paneles emisivos convertida a mapa de entorno. Sin archivos. */
function crearEntorno(renderer) {
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

  panel(0xffffff, 3.2, [12, 8], [0, 7, 0], [Math.PI / 2, 0, 0]); // cenital
  panel(0xd8ecf7, 1.5, [10, 8], [-7, 2, 2], [0, Math.PI / 2, 0]); // relleno frio
  panel(0xfff0dd, 0.9, [10, 8], [7, 2, -2], [0, -Math.PI / 2, 0]); // contra calido
  panel(0x2a3138, 1.0, [16, 16], [0, -3, 0], [-Math.PI / 2, 0, 0]); // piso

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
