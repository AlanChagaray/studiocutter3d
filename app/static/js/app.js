/* studioCutter3D — logica de las tres pantallas.
 *
 * Un solo archivo, sin bundler y sin dependencias: la app tiene que andar
 * abriendo el servidor local y nada mas. Cada pantalla se inicializa segun
 * `data-pagina` del body, asi que el codigo que no corresponde nunca corre.
 *
 * Dos cosas que no son casuales:
 *
 * - **El tema se aplica en la primera linea**, antes de DOMContentLoaded.
 *   Este script se carga sin `defer` en el `<head>` justo para eso: si
 *   esperara al DOM, la pagina parpadearia en claro antes de ponerse oscura.
 * - **Nada de `innerHTML` con datos que vienen del servidor o del usuario.**
 *   Las filas salen de un `<template>` y los textos se escriben con
 *   `textContent`. Es la unica forma de que un nombre de archivo hostil no
 *   pueda inyectar markup.
 */

/* ── Tema (corre inmediatamente) ─────────────────────────────────────────── */

(function aplicarTemaGuardado() {
  try {
    const guardado = localStorage.getItem('tema');
    if (guardado === 'claro' || guardado === 'oscuro') {
      document.documentElement.dataset.tema = guardado;
    }
  } catch (_) {
    /* modo privado o cookies bloqueadas: se usa el tema del sistema */
  }
})();

/* ── Estado de pantalla ──────────────────────────────────────────────────── */

/*
 * Sobrevive al cambio de seccion; NO sobrevive al refresh.
 *
 * La app es multi-pagina: cambiar de modulo recarga y se lleva puesto todo lo
 * que vive en una closure. `sessionStorage` lo devuelve, y el guard de abajo
 * implementa la otra mitad del pedido — F5 arranca de cero — mirando el tipo
 * de navegacion, que es la unica forma de distinguir "vine de otra pantalla"
 * de "recargue esta".
 *
 * Una clave POR PANTALLA y no una sola con secciones adentro: cada pantalla
 * lee y escribe la suya, y un JSON corrupto rompe una y no las tres.
 *
 * ⚠ Lo que NO se puede guardar es el `File` elegido del disco: no es
 * serializable y el navegador no deja reconstruirlo sin que el usuario lo
 * vuelva a elegir. Lo que si vuelve es todo lo demas —parametros, modo, el
 * trabajo ya lanzado y su resultado—, asi que al volver a la pantalla el
 * trabajo se recupera del SERVIDOR, que es la fuente de verdad.
 */

const PREFIJO_ESTADO = 'sc3d:';
const CLAVE_DUENIO = 'sc3d:duenio';

function olvidarTodoElEstado() {
  Object.keys(sessionStorage)
    .filter((k) => k.startsWith(PREFIJO_ESTADO))
    .forEach((k) => sessionStorage.removeItem(k));
}

(function limpiarEstadoSiCorresponde() {
  try {
    // Dos motivos para arrancar de cero, y el segundo no es cosmetico:
    //
    //  - F5, que es lo que se pidio.
    //  - Cambio de usuario. Cerrar sesion es una navegacion NORMAL, no una
    //    recarga, asi que sin este chequeo los 10 parametros del cortante y
    //    los ids de los trabajos del usuario anterior siguen en la pestaña
    //    para el que entre despues. En el login `data-usuario` va vacio, con
    //    lo cual pasar por ahi ya limpia.
    //
    // El dueño se lee de `<html>` y no de `<body>` porque esto corre antes de
    // DOMContentLoaded —el script va sin `defer`— y ahi `document.body` es
    // null.
    const duenioAhora = document.documentElement.dataset.usuario || '';
    const nav = performance.getEntriesByType('navigation')[0];
    if ((nav && nav.type === 'reload') || sessionStorage.getItem(CLAVE_DUENIO) !== duenioAhora) {
      olvidarTodoElEstado();
    }
    sessionStorage.setItem(CLAVE_DUENIO, duenioAhora);
  } catch (_) {
    /* sin sessionStorage no hay nada que limpiar */
  }
})();

function leerEstado(pantalla) {
  try {
    return JSON.parse(sessionStorage.getItem(PREFIJO_ESTADO + pantalla) || 'null') || {};
  } catch (_) {
    return {};
  }
}

function guardarEstado(pantalla, datos) {
  try {
    sessionStorage.setItem(PREFIJO_ESTADO + pantalla, JSON.stringify(datos));
  } catch (_) {
    /* modo privado o cuota llena: se pierde al navegar, no se rompe nada */
  }
}

/**
 * Que trabajo recordado se puede retomar al entrar a una pantalla.
 *
 * El trabajo guardado en `sessionStorage` se genero con la ENTRADA guardada.
 * Si la URL trae otra entrada —"seguir" desde un diseño nuevo en Convertir o
 * en Correcto— ese trabajo es del diseño anterior, y retomarlo hacia dos cosas
 * malas: pintaba el resultado viejo como si fuera de este archivo, y en el
 * cortante `retomarTrabajo` firmaba el estado ACTUAL como "ya generado", con
 * lo que el boton de generar quedaba apagado hasta que el usuario recargaba
 * (F5 es lo unico que borra el `sessionStorage`). Sin `origen` en la URL se
 * volvio a la pantalla por el menu, y ahi retomar es justo lo que se quiere.
 *
 * Es una funcion de modulo y no una linea dentro de cada `iniciar*` para que
 * las dos pantallas apliquen la misma regla y un test pueda exigirla.
 */
function trabajoQueSigueVigente(recordado, origenDeUrl) {
  if (origenDeUrl && origenDeUrl !== recordado.origen) return null;
  return recordado.trabajo || null;
}

/* ── Utilidades ──────────────────────────────────────────────────────────── */

const $ = (sel, raiz = document) => raiz.querySelector(sel);
const $$ = (sel, raiz = document) => Array.from(raiz.querySelectorAll(sel));
const esperar = (ms) => new Promise((r) => setTimeout(r, ms));

const MS_POLLING_RAPIDO = 800;
const MS_POLLING_LENTO = 2000;
const MS_HASTA_LENTO = 30000;
const MS_LIMITE = 150000;

const PISTA_LISTA = 'Los archivos ya estan en el servidor: se bajan cuando quieras.';

const PISTA_SIN_PREVIEW =
  'No se pudo mostrar la vista previa. Los archivos estan completos igual.';

// "La geometria" y no "lo que estas viendo": el color de la pieza lo elige la
// paleta del visor y no viaja al archivo. La forma si es exactamente esta.
const PISTA_CON_PREVIEW = 'Lo que estas viendo es exactamente la geometria que se descarga.';

// Rendir a 2048 px y subir la foto tarda, pero no tanto. Pasado esto se asume
// que del otro lado no va a contestar nadie y el ZIP sigue sin la imagen.
const MS_ESPERA_FOTO = 30000;

// La clave de la foto. Se escribe una sola vez porque la comparan cuatro
// lugares distintos, y un typo en uno solo rompe la entrada en silencio.
const CLAVE_FOTO = 'jpg_vista';

// El mismo tope que `app.archivos.MAX_DISENOS`. Se repite aca —y hay un test
// que lo exige— porque el front tiene que poder avisar ANTES de subir 50 MB
// para que el servidor conteste 422. La verdad sigue siendo la del servidor:
// esto solo evita el viaje.
const MAX_DISENOS = 25;

function mostrar(el, visible = true) {
  if (el) el.classList.toggle('oculto', !visible);
}

function texto(el, valor) {
  if (el) el.textContent = valor;
}

function mm(valor, decimales = 2) {
  return valor === null || valor === undefined ? '—' : `${Number(valor).toFixed(decimales)} mm`;
}

function urlArchivo(id, clave) {
  return `/api/trabajos/${encodeURIComponent(id)}/archivo/${encodeURIComponent(clave)}`;
}

function urlZip(id) {
  return `/api/trabajos/${encodeURIComponent(id)}/zip`;
}

/** Error con la forma uniforme de la API, para poder ramificar sobre el codigo. */
class ErrorDeApi extends Error {
  constructor(cuerpo, estado) {
    const datos = (cuerpo && cuerpo.error) || {};
    super(datos.mensaje || 'No se pudo completar la operacion.');
    this.codigo = datos.codigo || 'interno';
    this.detalle = datos.detalle || {};
    this.estado = estado;
  }
}

async function comoJson(respuesta) {
  let cuerpo = null;
  try {
    cuerpo = await respuesta.json();
  } catch (_) {
    cuerpo = null;
  }
  if (!respuesta.ok) throw new ErrorDeApi(cuerpo, respuesta.status);
  return cuerpo;
}

async function pedirJson(url, opciones = {}) {
  return comoJson(
    await fetch(url, { headers: { Accept: 'application/json' }, ...opciones })
  );
}

/** POST multipart con progreso real de subida. `fetch` no lo expone. */
function enviar(url, datos, alProgreso) {
  return new Promise((resolver, rechazar) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.responseType = 'json';
    if (alProgreso) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) alProgreso(e.loaded / e.total);
      };
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolver(xhr.response);
      else rechazar(new ErrorDeApi(xhr.response, xhr.status));
    };
    xhr.onerror = () => rechazar(new Error('Se corto la conexion con el servidor.'));
    xhr.send(datos);
  });
}

/**
 * Consulta el trabajo hasta que termina.
 *
 * Rapido los primeros 30 s —que es cuando el usuario esta mirando— y lento
 * despues. Corta a los 150 s: mas que el timeout del servidor, asi que si se
 * llega hasta aca es que el servidor dejo de responder, no que el trabajo
 * tarda.
 */
async function sondear(id, alCambiar) {
  const inicio = Date.now();
  for (;;) {
    const trabajo = await pedirJson(`/api/trabajos/${encodeURIComponent(id)}`);
    if (alCambiar) alCambiar(trabajo);
    if (trabajo.estado === 'listo' || trabajo.estado === 'error') return trabajo;
    const transcurrido = Date.now() - inicio;
    if (transcurrido > MS_LIMITE) {
      throw new Error('El servidor dejo de responder. Recarga la pagina para ver el estado.');
    }
    await esperar(transcurrido < MS_HASTA_LENTO ? MS_POLLING_RAPIDO : MS_POLLING_LENTO);
  }
}

/** Convierte un trabajo terminado en error a una excepcion. */
function siFalloTirar(trabajo) {
  if (trabajo.estado !== 'error') return trabajo;
  const e = new ErrorDeApi({ error: trabajo.error || {} }, 422);
  throw e;
}

/* ── Avisos ──────────────────────────────────────────────────────────────── */

function avisar(el, tipo, mensaje) {
  if (!el) return;
  el.className = `aviso aviso--${tipo}`;
  el.textContent = mensaje;
  mostrar(el, true);
}

/* ── Dropzone reutilizable ───────────────────────────────────────────────── */

function conectarZona(zona, input, alElegir, alQuitar) {
  if (!zona || !input) return;
  const boton = $('#elegir', zona) || $('#elegir');
  if (boton) boton.addEventListener('click', () => input.click());
  // Antes esto exigia `e.target === zona`, asi que el picker solo se abria
  // apretando el padding: el icono, el titulo y el texto de ayuda —que son casi
  // toda la superficie de la zona— no hacian nada. Ahora abre desde cualquier
  // punto menos los controles propios, que ya tienen su comportamiento.
  zona.addEventListener('click', (e) => {
    if (!e.target.closest('button, a, input, select, label')) input.click();
  });
  ['dragenter', 'dragover'].forEach((evt) =>
    zona.addEventListener(evt, (e) => {
      e.preventDefault();
      zona.classList.add('dropzone--activa');
    })
  );
  ['dragleave', 'drop'].forEach((evt) =>
    zona.addEventListener(evt, (e) => {
      e.preventDefault();
      zona.classList.remove('dropzone--activa');
    })
  );
  zona.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files.length) alElegir(e.dataTransfer.files);
  });
  input.addEventListener('change', () => {
    if (input.files.length) alElegir(input.files);
  });

  const quitar = $('#quitar');
  if (quitar && alQuitar) {
    quitar.addEventListener('click', (e) => {
      e.stopPropagation();
      // Limpiar `value` es lo que hace que volver a elegir EL MISMO archivo
      // vuelva a disparar `change`. Sin esto la "x" parece rota justo en el
      // caso mas comun: quitar y reponer el mismo archivo.
      input.value = '';
      alQuitar();
    });
  }
}

/* ── Interruptor de tema ─────────────────────────────────────────────────── */

function iniciarTema() {
  const boton = $('#boton-tema');
  const sw = $('#switch-tema');
  if (!boton || !sw) return;

  const oscuroAhora = () => {
    const elegido = document.documentElement.dataset.tema;
    if (elegido) return elegido === 'oscuro';
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  };

  const pintar = () => sw.setAttribute('aria-checked', String(oscuroAhora()));
  pintar();

  boton.addEventListener('click', () => {
    const nuevo = oscuroAhora() ? 'claro' : 'oscuro';
    document.documentElement.dataset.tema = nuevo;
    try {
      localStorage.setItem('tema', nuevo);
    } catch (_) {
      /* sin persistencia: el cambio vale para esta pagina igual */
    }
    pintar();
    document.dispatchEvent(new CustomEvent('tema:cambio', { detail: { oscuro: nuevo === 'oscuro' } }));
  });
}

/* ── F1: convertidor ─────────────────────────────────────────────────────── */

/**
 * Cola de conversion con destino por archivo.
 *
 * La cola se arma primero y se convierte despues, en dos pasos: el destino
 * (JPG o SVG) es una decision por archivo, y no se puede decidir mientras la
 * conversion ya arranco. Los botones de arriba fijan el destino de todo lo
 * que este pendiente —el caso comun, todos iguales— y el selector de cada
 * fila lo pisa cuando hacen falta mezclados.
 *
 * Los archivos se mandan **de a uno y en serie**: asi cada uno tiene su
 * propio resultado y su propio error, y uno que falla no arrastra al resto.
 */
function iniciarConversor() {
  const molde = $('#molde-fila');
  const cola = $('#cola');
  const botonConvertir = $('#convertir');
  const filas = [];
  let convirtiendo = false;

  const botonesDestino = $$('.segmentado button[data-destino]');
  const destinoGlobal = () => {
    const activo = botonesDestino.find((b) => b.getAttribute('aria-pressed') === 'true');
    return activo ? activo.dataset.destino : 'jpg';
  };
  const pendientes = () => filas.filter((f) => f.estado === 'pendiente');

  conectarZona($('#zona'), $('#entrada'), (archivos) => {
    Array.from(archivos).forEach(agregar);
    mostrar($('#panel-cola'), true);
    refrescar();
  });

  botonesDestino.forEach((b) =>
    b.addEventListener('click', () => {
      botonesDestino.forEach((o) => o.setAttribute('aria-pressed', String(o === b)));
      pendientes().forEach((f) => {
        f.destino = b.dataset.destino;
        $('[data-rol="destino"]', f.nodo).value = f.destino;
      });
    })
  );

  botonConvertir.addEventListener('click', convertirTodo);

  function agregar(archivo) {
    const nodo = molde.content.firstElementChild.cloneNode(true);
    const ext = (archivo.name.split('.').pop() || '?').slice(0, 4).toUpperCase();
    texto($('[data-rol="ext"]', nodo), ext);
    texto($('[data-rol="nombre"]', nodo), archivo.name);
    texto($('[data-rol="peso"]', nodo), `${(archivo.size / 1024).toFixed(0)} KB`);

    const fila = { archivo, nodo, destino: destinoGlobal(), estado: 'pendiente', id: null };
    const selector = $('[data-rol="destino"]', nodo);
    selector.value = fila.destino;
    selector.addEventListener('change', () => {
      fila.destino = selector.value;
    });

    cola.appendChild(nodo);
    filas.push(fila);
  }

  function refrescar() {
    const faltan = pendientes().length;
    botonConvertir.disabled = convirtiendo || faltan === 0;
    texto($('#resumen-cola'), `${filas.length - faltan} de ${filas.length}`);
    texto(
      $('#pista-cola'),
      faltan === 0
        ? 'No queda nada pendiente. Podes seguir agregando archivos.'
        : `${faltan} archivo(s) sin convertir. Elegi el destino de cada uno o usa los botones de arriba.`
    );
    pintarPie();
  }

  /** El pie ofrece las dos continuaciones segun lo que haya salido. */
  function pintarPie() {
    const listos = filas.filter((f) => f.estado === 'listo');
    const ultimo = (formato) => [...listos].reverse().find((f) => f.destino === formato);
    const jpg = ultimo('jpg');
    const svg = ultimo('svg');
    const aLineas = $('#seguir-lineas');
    const aCortante = $('#seguir-cortante');

    if (jpg) aLineas.href = `/lineas?origen=${encodeURIComponent(jpg.id)}`;
    if (svg) aCortante.href = `/cortante?origen=${encodeURIComponent(svg.id)}`;
    mostrar(aLineas, Boolean(jpg));
    mostrar(aCortante, Boolean(svg));
    texto(
      $('#pista-pie'),
      svg
        ? 'El SVG es lo unico que entra al cortante.'
        : 'El JPG es la entrada de la correccion de lineas.'
    );
    mostrar($('#pie'), listos.length > 0);
  }

  async function convertirTodo() {
    convirtiendo = true;
    refrescar();
    for (const fila of pendientes()) {
      await convertirUno(fila);
      refrescar();
    }
    convirtiendo = false;
    refrescar();
  }

  async function convertirUno(fila) {
    const chip = $('[data-rol="chip"]', fila.nodo);
    const barra = $('[data-rol="barra"]', fila.nodo);
    const selector = $('[data-rol="destino"]', fila.nodo);
    const datos = new FormData();
    datos.append('archivo', fila.archivo, fila.archivo.name);
    datos.append('formato', fila.destino);

    selector.disabled = true;
    chip.className = 'chip chip--proceso';
    chip.textContent = fila.destino === 'svg' ? 'vectorizando' : 'convirtiendo';

    try {
      const trabajo = await enviar('/api/conversor', datos, (f) => {
        barra.style.width = `${Math.round(f * 92)}%`;
      });
      barra.style.width = '100%';
      chip.className = 'chip chip--ok';
      chip.textContent = 'listo';
      fila.estado = 'listo';
      fila.id = trabajo.id;

      const bajar = $('[data-rol="bajar"]', fila.nodo);
      bajar.href = urlArchivo(trabajo.id, fila.destino);
      texto($('[data-rol="ext-bajar"]', bajar), fila.destino.toUpperCase());
      mostrar(bajar, true);
    } catch (e) {
      barra.style.width = '100%';
      barra.style.background = 'var(--rojo)';
      chip.className = 'chip chip--error';
      chip.textContent = e.codigo === 'formato_no_soportado' ? 'formato invalido' : 'error';
      chip.title = e.message;
      fila.estado = 'error';
    }
  }
}

/* ── Encadenado entre pantallas ──────────────────────────────────────────── */

/**
 * Nombre real del archivo con el que arranco un trabajo.
 *
 * Antes las pantallas de destino mostraban un literal —"viene del conversor"—
 * que no decia nada util: con tres pestañas abiertas no habia forma de saber
 * cual era cual. El servidor conserva el nombre original saneado, asi que
 * alcanza con preguntarselo.
 *
 * De paso VALIDA el origen: si el id no existe o vencio, se sabe al entrar y
 * no al apretar el boton.
 */
async function nombreDeTrabajo(id, siNoHay) {
  try {
    const t = await pedirJson(`/api/trabajos/${encodeURIComponent(id)}`);
    return t.nombre_base || siNoHay;
  } catch (_) {
    return null;
  }
}

/* ── F2: correccion de lineas ────────────────────────────────────────────── */

function iniciarLineas() {
  const estado = $('#estado');
  const procesar = $('#procesar');
  const swMacizos = $('#switch-macizos');
  const swNormalizar = $('#switch-normalizar');
  const camposNormalizar = $('#campos-normalizar');
  /* Los ids salen del macro `campo_parametro`, igual que en la pantalla del
     cortante: un campo por nombre de parametro, sin lista aparte. */
  const CAMPOS_NORMALIZAR = ['ancho_trazo_mm', 'lado_mayor_mm'];

  const recordado = leerEstado('lineas');
  let archivo = null;
  const origenDeUrl = new URLSearchParams(location.search).get('origen');
  let origen = origenDeUrl || recordado.origen || null;
  let trabajoId = trabajoQueSigueVigente(recordado, origenDeUrl);

  const encendida = (sw) => sw.getAttribute('aria-checked') === 'true';

  const recordar = () =>
    guardarEstado('lineas', {
      origen,
      trabajo: trabajoId,
      macizos: swMacizos.getAttribute('aria-checked'),
      normalizar: swNormalizar.getAttribute('aria-checked'),
      medidas: Object.fromEntries(CAMPOS_NORMALIZAR.map((n) => [n, $(`#p-${n}`).value])),
    });

  if (recordado.macizos) swMacizos.setAttribute('aria-checked', recordado.macizos);
  if (recordado.normalizar) swNormalizar.setAttribute('aria-checked', recordado.normalizar);
  if (recordado.medidas) {
    for (const nombre of CAMPOS_NORMALIZAR) {
      const guardado = recordado.medidas[nombre];
      if (guardado) $(`#p-${nombre}`).value = guardado;
    }
  }
  mostrar(camposNormalizar, encendida(swNormalizar));

  $('#boton-macizos').addEventListener('click', () => {
    swMacizos.setAttribute('aria-checked', String(!encendida(swMacizos)));
    recordar();
  });

  $('#boton-normalizar').addEventListener('click', () => {
    const activo = !encendida(swNormalizar);
    swNormalizar.setAttribute('aria-checked', String(activo));
    mostrar(camposNormalizar, activo);
    recordar();
  });

  for (const nombre of CAMPOS_NORMALIZAR) {
    $(`#p-${nombre}`).addEventListener('change', recordar);
  }

  function ponerChip(nombre) {
    texto($('#nombre-archivo'), nombre);
    mostrar($('#chip-archivo'), true);
  }

  conectarZona(
    $('#zona'),
    $('#entrada'),
    (archivos) => {
      archivo = archivos[0];
      origen = null; // un archivo del disco pisa al que venia encadenado
      ponerChip(archivo.name);
      $('#img-original').src = URL.createObjectURL(archivo);
      mostrar($('#panel-comparador'), true);
      procesar.disabled = false;
      recordar();
    },
    () => {
      archivo = null;
      origen = null;
      mostrar($('#chip-archivo'), false);
      $('#img-original').removeAttribute('src');
      mostrar($('#panel-comparador'), false);
      procesar.disabled = true;
      recordar();
    }
  );

  if (origen) restaurarOrigen(origen);
  if (trabajoId) retomarTrabajo(trabajoId);

  async function restaurarOrigen(id) {
    const nombre = await nombreDeTrabajo(id, 'viene del conversor');
    if (nombre === null) {
      // El origen no existe o vencio: mejor saberlo ahora que al enviar.
      origen = null;
      recordar();
      avisar(estado, 'alerta', 'El archivo de la pantalla anterior ya no esta disponible.');
      return;
    }
    $('#img-original').src = urlArchivo(id, 'jpg');
    ponerChip(nombre);
    mostrar($('#panel-comparador'), true);
    procesar.disabled = false;
    recordar(); // mismo motivo que en el cortante: lo recordado es ESTE origen
  }

  /** Vuelve a enganchar un trabajo que quedo corriendo al cambiar de seccion. */
  async function retomarTrabajo(id) {
    try {
      const t = await pedirJson(`/api/trabajos/${encodeURIComponent(id)}`);
      if (t.estado === 'error') return;
      if (t.estado === 'listo') {
        pintarResultado(t);
        return;
      }
      avisar(estado, 'info', `${t.etapa}…`);
      pintarResultado(
        siFalloTirar(await sondear(id, (x) => avisar(estado, 'info', `${x.etapa}…`)))
      );
    } catch (_) {
      trabajoId = null; // vencio por TTL: se olvida sin molestar al usuario
      recordar();
    }
  }

  procesar.addEventListener('click', async () => {
    procesar.disabled = true;
    const datos = new FormData();
    if (archivo) datos.append('archivo', archivo, archivo.name);
    else if (origen) datos.append('origen', origen);
    datos.append('contornear_macizos', swMacizos.getAttribute('aria-checked'));
    datos.append('normalizar_trazo', swNormalizar.getAttribute('aria-checked'));
    /* Las dos medidas viajan siempre, encendida o no la opcion: el servidor las
       valida igual y asi el pedido no depende del estado de un switch. */
    for (const nombre of CAMPOS_NORMALIZAR) datos.append(nombre, $(`#p-${nombre}`).value);

    try {
      avisar(estado, 'info', 'Procesando la imagen…');
      const lanzado = await enviar('/api/lineas', datos);
      trabajoId = lanzado.id;
      recordar();
      const trabajo = siFalloTirar(
        await sondear(lanzado.id, (t) => avisar(estado, 'info', `${t.etapa}…`))
      );
      pintarResultado(trabajo);
    } catch (e) {
      avisar(estado, 'error', e.message);
    } finally {
      procesar.disabled = false;
    }
  });

  function pintarResultado(trabajo) {
    mostrar(estado, false);
    /* Los dos avisos se apagan de entrada: esta funcion corre de nuevo en cada
       corrida y uno que solo sabe encenderse deja el cartel de la anterior. */
    mostrar($('#aviso-macizos'), false);
    mostrar($('#aviso-normalizar'), false);
    $('#img-corregida').src = `${urlArchivo(trabajo.id, 'png')}?v=${Date.now()}`;
    const r = trabajo.reporte || {};
    texto($('#d-umbral'), String(r.umbral_usado ?? '—'));
    texto($('#d-ancho'), `${r.ancho_trazo_px ?? '—'} px`);
    texto($('#d-zonas'), String(r.zonas_contorneadas ?? 0));
    texto($('#d-area'), `${(r.area_contorneada_px ?? 0).toLocaleString('es-AR')} px`);
    /* La escala de trabajo se declara siempre, cambie o no: es la escala en la
       que estan todos los pixeles de este panel y la del JPG que se baja. */
    const [anchoPx, altoPx] = r.tamano_usado || [];
    let escala = '';
    if (r.fue_ampliada) escala = ` (ampliada ${r.factor_ampliacion}×)`;
    else if (r.fue_reducida) escala = ' (reducida por tamaño)';
    texto($('#d-resolucion'), anchoPx ? `${anchoPx}×${altoPx} px${escala}` : '—');
    mostrar($('#panel-declaracion'), true);

    const normalizado = !!r.normalizacion_activa;
    const logradoPx = r.ancho_logrado_px ?? 0;
    texto($('#d-objetivo'), normalizado ? `${logradoPx} px = ${r.ancho_objetivo_mm} mm` : '—');
    texto(
      $('#d-delta'),
      normalizado
        ? `+${(r.area_engrosada_px ?? 0).toLocaleString('es-AR')} / ` +
            `−${(r.area_afinada_px ?? 0).toLocaleString('es-AR')} px`
        : '—'
    );
    for (const fila of document.querySelectorAll('[data-rol="fila-normalizada"]')) {
      mostrar(fila, normalizado);
    }

    if (r.zonas_contorneadas > 0) {
      texto(
        $('#texto-macizos'),
        `Se reemplazaron ${r.zonas_contorneadas} zona(s) maciza(s) por su contorno, ` +
          `afectando ${(r.area_contorneada_px ?? 0).toLocaleString('es-AR')} px.`
      );
      mostrar($('#aviso-macizos'), true);
    }

    /* La suposicion del lado mayor se repite aca y no solo en el formulario: es
       lo unico que ata este resultado al cortante que venga despues, y el
       usuario lo lee cuando ya se olvido de que lo eligio. */
    if (normalizado) {
      const protegida = r.area_protegida_px ?? 0;
      texto(
        $('#texto-normalizar'),
        `Los trazos quedaron todos en ${logradoPx} px, que son ${r.ancho_objetivo_mm} mm ` +
          `si el cortante se genera con un lado mayor de ${r.lado_mayor_supuesto_mm} mm. ` +
          `Se engrosaron ${(r.area_engrosada_px ?? 0).toLocaleString('es-AR')} px y se afinaron ` +
          `${(r.area_afinada_px ?? 0).toLocaleString('es-AR')} px.` +
          (protegida > 0
            ? ` Con el contorneado apagado, ${protegida.toLocaleString('es-AR')} px de zona ` +
              'maciza quedaron como estaban: emparejarlos los convertiria en lineas.'
            : '') +
          (r.fue_ampliada
            ? ` Para que el trazo saliera liso la imagen se trabajo ampliada ` +
              `${r.factor_ampliacion}×: el JPG y el SVG salen a ${anchoPx}×${altoPx} px.`
            : '')
      );
      mostrar($('#aviso-normalizar'), true);
    }

    /* Se baja el JPG y no el PNG con el que trabajo la etapa: es lo que el
       usuario abre en Paint para retocar, y lo unico que Correcto acepta de
       vuelta. El PNG sigue siendo lo que se vectoriza y lo que se ve arriba. */
    $('#bajar').href = urlArchivo(trabajo.id, 'jpg_editable');
    $('#bajar-svg').href = urlArchivo(trabajo.id, 'svg');
    $('#seguir').href = `/cortante?origen=${encodeURIComponent(trabajo.id)}`;
    mostrar($('#pie'), true);
  }
}

/* ── F3: crear cortante ──────────────────────────────────────────────────── */

/* ── Vista previa 3D + foto cenital, compartida por las pantallas ────────── */

/**
 * Todo lo que rodea al visor: las dos vistas, las paletas y la foto descargable.
 *
 * **Vive a nivel de modulo y no adentro de una pantalla porque tiene dos.** El
 * cortante la usa despues de generar la geometria; el post, despues de leer un
 * `.3mf` o un `.stl` que ya existia. Que las dos fotos salgan iguales no es algo
 * que se pueda sostener con dos copias parecidas de estas 200 lineas: es el
 * mismo principio que ya sostiene `preview3d.js`, que tampoco tiene una version
 * por pantalla.
 *
 * `preview3d.js` es modulo ES y este archivo es script clasico: **no pueden
 * importarse**. El unico canal es el bus de `CustomEvent` sobre `document`, y
 * los nombres de los eventos siguen siendo `cortante:*` en las dos pantallas.
 * No se renombraron a proposito: son un contrato con el visor y con los tests,
 * y cambiarlos costaria tocar `preview3d.js` sin ganar nada. Aca "cortante"
 * nombra a la pieza que se esta mirando, no a la pantalla que la pidio.
 *
 * Los ganchos del DOM son los mismos ids en las dos pantillas (`#visor`,
 * `#lienzo`, `#paleta`, `#paleta-pieza`, `#paleta-fondo`, `#pista-imagen`,
 * `#botones-descarga`), tambien por contrato: `preview3d.js` los resuelve una
 * sola vez a nivel de modulo y no tiene namespace por pantalla.
 *
 * @param {object} opciones
 * @param {() => string|null} opciones.obtenerTrabajoId  El trabajo vigente. Es
 *   una funcion y no un valor porque cambia con cada generacion, y la foto se
 *   sube contra el trabajo que hay **al momento de pedirla**.
 * @param {string} [opciones.vistaInicial]  `'imagen'` o `'3d'` (default).
 * @param {(vista: string) => void} [opciones.alCambiarVista]  Para que la
 *   pantalla persista la eleccion en su propio `sessionStorage`.
 * @param {(id: string) => string} [opciones.urlFoto]  Donde va la foto. Cortante
 *   la sube al trabajo; post la sube al diseño que esta mirando, asi que pasa la
 *   suya. El visor no elige: rinde y manda adonde le digan.
 * @param {boolean} [opciones.subirAlDescargar]  Si el clic en la descarga tiene
 *   que rendir y subir la foto ANTES de navegar. En cortante si —la foto depende
 *   de los colores, que se pueden cambiar hasta el ultimo segundo—. En post no:
 *   las fotos ya se subieron todas al armar el lote, y volver a rendir al bajar
 *   subiria la del diseño que quedo en el visor encima de la del que se pidio.
 */
function iniciarVistaPrevia3D({
  obtenerTrabajoId,
  vistaInicial,
  alCambiarVista,
  urlFoto = (id) => `/api/trabajos/${encodeURIComponent(id)}/imagen`,
  subirAlDescargar = true,
}) {
  // El default es `3d` y se valida contra la lista: un valor raro en
  // `sessionStorage` no puede dejar la pantalla sin ninguna vista visible.
  let vista = vistaInicial === 'imagen' ? 'imagen' : '3d';
  // Si la vista imagen puede producir la foto. Lo dice `cortante:imagen`,
  // que llega tanto cuando carga el modelo como cuando no pudo.
  let hayFoto = false;
  let exportando = false;

  // El visor solo cambia el texto de la pista. Que no haya podido pintar no
  // deja a nadie sin poder bajar un archivo que esta perfecto.
  document.addEventListener('cortante:preview', (e) =>
    texto($('#pista-descargas'), e.detail.ok ? PISTA_CON_PREVIEW : PISTA_SIN_PREVIEW)
  );

  /* ── Exportar la foto: pedirsela al visor y recien despues bajar ────────── */

  /**
   * El handshake con `preview3d.js`, que es modulo y no se puede importar.
   *
   * El unico canal es el bus de eventos: se emite `cortante:exportar` y se
   * espera UN `cortante:imagen`. La promesa se resuelve con el resultado, y no
   * hace falta timeout porque del otro lado el listener esta registrado
   * siempre — hasta sin WebGL contesta, con `ok: false`.
   */
  function pedirFoto(destino) {
    return new Promise((resolver) => {
      // ⚠ El `{once}` se registra ANTES del despacho, y no es estilo: las
      // respuestas de "no hay vista" y "todavia no hay modelo" salen
      // **sincronicamente** adentro del `dispatchEvent`. Al reves, el handshake
      // se rompe entero y en silencio.
      let listo = false;
      const contestar = (detalle) => {
        if (listo) return;
        listo = true;
        document.removeEventListener('cortante:imagen', alLlegar);
        resolver(detalle);
      };
      // ⚠ Solo la respuesta a ESTA exportacion, no cualquier `cortante:imagen`.
      // El mismo evento lo emite tambien la carga del modelo, asi que un `.glb`
      // que termine de cargar entre el clic y la respuesta del PUT resolvia
      // esta promesa antes de tiempo: la descarga arrancaba con la subida
      // todavia en vuelo y el ZIP se llevaba la foto vieja — justo lo que el
      // `await` de aca existe para impedir.
      const alLlegar = (e) => {
        if (e.detail.motivo === 'exportar') contestar(e.detail);
      };
      document.addEventListener('cortante:imagen', alLlegar);
      // El ZIP **no necesita al visor**: los archivos ya estan enteros en el
      // servidor. Sin este tope, un `preview3d.js` que no llego a evaluarse —o
      // un `fetch` que se cuelga sin rechazar— dejaba esta promesa sin asentar
      // para siempre, y con ella el boton deshabilitado y `exportando` clavado
      // en true. O sea: el visor roto se llevaba puesta una descarga que no
      // dependia de el. Que la promesa SIEMPRE asiente es lo que lo evita.
      setTimeout(
        () => contestar({ ok: false, error: 'la vista previa no respondio a tiempo' }),
        MS_ESPERA_FOTO
      );
      document.dispatchEvent(new CustomEvent('cortante:exportar', { detail: { destino } }));
    });
  }

  /**
   * Sube la foto fresca y despues navega. **Ese orden es todo el punto.**
   *
   * Lo que se baja tiene que ser lo que se esta viendo: la foto depende del
   * color de la pieza y del fondo, que se pueden cambiar en cualquier momento.
   * Por eso el link no navega solo — se intercepta, se rinde y se sube, y
   * recien ahi se pide el archivo al servidor.
   */
  async function exportarYBajar(url, seguirSinFoto) {
    if (exportando) return;
    if (!obtenerTrabajoId()) {
      // Puede pasar: si `retomarTrabajo` falla despues de haber pintado el
      // panel, el boton queda visible con el trabajo ya olvidado. Un boton que
      // no hace nada ni dice nada es el peor de los dos mundos.
      texto($('#pista-descargas'), 'Se perdio la referencia al trabajo: volve a generarlo.');
      return;
    }
    exportando = true;
    const boton = $('#bajar-todo');
    if (boton) boton.disabled = true;
    try {
      const resultado = await pedirFoto(urlFoto(obtenerTrabajoId()));
      if (!resultado.ok) {
        // Degradacion declarada, nunca silenciosa — y con el motivo REAL.
        //
        // Habia un texto fijo que decia "este navegador no pudo generarla", y
        // era falso en tres de los cinco motivos posibles: que el modelo
        // todavia se este cargando, que el servidor rechace la foto o que se
        // haya agotado la espera no tienen nada que ver con el navegador.
        // Decir mal por que fallo algo es peor que no decirlo: manda a buscar
        // el problema al lugar equivocado.
        if (!seguirSinFoto) {
          texto($('#pista-descargas'), `No se pudo preparar la imagen: ${resultado.error}.`);
          return;
        }
        texto($('#pista-descargas'), `El ZIP va sin la imagen: ${resultado.error}.`);
      } else {
        texto($('#pista-descargas'), PISTA_LISTA);
      }
      window.location.assign(url);
    } finally {
      exportando = false;
      if (boton) boton.disabled = false;
    }
  }

  document.addEventListener('click', (e) => {
    if (!subirAlDescargar) return;
    const foto = e.target.closest(`#botones-descarga a[data-clave="${CLAVE_FOTO}"]`);
    if (foto && foto.href) {
      e.preventDefault();
      // Sin foto no hay nada que bajar: el destino ES la foto.
      exportarYBajar(foto.href, false);
      return;
    }
    if (e.target.closest('#bajar-todo')) {
      e.preventDefault();
      // El ZIP baja igual: los otros archivos estan enteros en el servidor y
      // no dependen del visor. Lo que falte, se dice.
      const id = obtenerTrabajoId();
      if (id) exportarYBajar(urlZip(id), true);
    }
  });

  // Lo que decide si la entrada del JPG se ofrece: **solo** el aviso de carga.
  //
  // Un fallo de EXPORTACION no dice nada sobre si la foto se puede producir —
  // "ya hay una exportacion en curso" o un rechazo del servidor son
  // transitorios—, y escondiendo la entrada ante eso quedaba un boton que
  // desaparece al tocarlo y solo vuelve regenerando el cortante.
  document.addEventListener('cortante:imagen', (e) => {
    if (e.detail.motivo !== 'carga') return;
    hayFoto = e.detail.ok;
    if (!subirAlDescargar) return;
    const id = obtenerTrabajoId();
    const a = $(`#botones-descarga a[data-clave="${CLAVE_FOTO}"]`);
    if (!a) return;
    if (hayFoto && id) a.href = urlArchivo(id, CLAVE_FOTO);
    else a.removeAttribute('href');
    mostrar(a, hayFoto && Boolean(id));
  });

  // El color de la PIEZA es uno para las dos vistas y se ofrece en las dos
  // (`#paleta` flota sobre el visor 3D, `#paleta-pieza` va en la barra de la
  // imagen). El del FONDO existe solo en la imagen.
  //
  // ⚠ Las dos paletas arrancan en la MISMA muestra (blanco), y eso antes no se
  // podia: con el estudio de foto viejo la pieza casi no proyectaba sombra
  // sobre el fondo y blanco sobre blanco no se distinguia, asi que el fondo
  // arrancaba en la segunda (`indiceDefecto: 1`). Con la luz del visor la
  // pieza tiene sombra propia y se lee sobre blanco — y blanco es lo que se
  // pidio para el fondo de la foto.
  //
  // Ninguna de las tres declara cual muestra arranca elegida, y eso es el
  // arreglo: el default lo decide **solo** el `activo` del macro, del lado del
  // template. Hasta este ciclo el fondo lo declaraba tambien aca y las dos
  // verdades se desincronizaron — el HTML servido marcaba blanco y la pantalla
  // mostraba gris, porque `aplicar()` reescribe `aria-pressed` al arrancar y el
  // JS siempre gana. El detalle completo esta en `iniciarPaleta`.
  iniciarPaleta({
    selectores: ['#paleta', '#paleta-pieza'],
    clave: 'color-visor',
    evento: 'cortante:color',
  });
  iniciarPaleta({
    selectores: ['#paleta-fondo'],
    clave: 'color-fondo',
    evento: 'cortante:fondo',
  });

  /**
   * Las dos vistas previas de la misma geometria.
   *
   * `3d` es el visor de siempre y no cambia en nada; `imagen` es la foto
   * cenital descargable. Lo unico que hace esta funcion es mostrar una y
   * esconder la otra: quien las dibuja es `preview3d.js`, que tiene el
   * contexto de WebGL.
   *
   * El aviso `cortante:vista` no es decorativo. La vista imagen **no tiene
   * loop de render** —es una toma fija, se rinde solo cuando algo cambia—, asi
   * que mientras estuvo escondida su canvas midio 0 y no pudo pintarse. El
   * aviso es lo que la despierta al volver.
   */
  function iniciarVistas() {
    const botones = $$('.segmentado--vistas button[data-vista]');
    if (!botones.length) return;

    const aplicar = (elegida) => {
      vista = elegida;
      botones.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.vista === elegida)));
      mostrar($('#visor'), elegida === '3d');
      mostrar($('#vista-imagen'), elegida === 'imagen');
      mostrar($('#pista-imagen'), elegida === 'imagen');
      document.dispatchEvent(new CustomEvent('cortante:vista', { detail: { vista: elegida } }));
    };

    botones.forEach((b) =>
      b.addEventListener('click', () => {
        aplicar(b.dataset.vista);
        if (alCambiarVista) alCambiarVista(vista);
      })
    );
    aplicar(vista);
  }

  iniciarVistas();

  return {
    /** Si la foto se puede producir ahora mismo. Lo lee `pintarResultado`. */
    get hayFoto() {
      return hayFoto;
    },

    /** La vista elegida, para que la pantalla la persista. */
    get vista() {
      return vista;
    },

    /**
     * ⚠ Se llama ANTES de pintar un resultado nuevo, y no es opcional.
     *
     * `hayFoto` es de la generacion ANTERIOR hasta que el `.glb` nuevo termine
     * de cargar, y dejarlo en `true` abre una ventana de segundos en la que la
     * entrada del JPG ya esta visible y apuntando al trabajo nuevo mientras el
     * visor todavia tiene la pieza vieja. Un clic ahi sube la foto de la pieza
     * anterior como `jpg_vista` del trabajo nuevo — el unico punto de toda la
     * cadena donde lo que se baja NO seria lo que se esta viendo, que es la
     * invariante que sostiene el diseño entero de estas pantallas. La vuelve a
     * subir el aviso `cortante:imagen` de motivo `carga`.
     */
    reiniciar() {
      hayFoto = false;
    },

    /** Le avisa al visor que hay un `.glb` nuevo para cargar. */
    anunciar(url) {
      document.dispatchEvent(new CustomEvent('cortante:listo', { detail: { url } }));
    },

    /**
     * Anuncia un `.glb` y **espera** a que el visor lo tenga cargado.
     *
     * Es `anunciar` mas la espera, y existe para el lote de post: las fotos se
     * sacan de a una, y sacar la segunda antes de que el segundo modelo este
     * cargado fotografiaria el primero. `preview3d.js` contesta siempre —hasta
     * sin WebGL, con `ok:false`— asi que esta promesa siempre asienta.
     */
    cargar(url) {
      return new Promise((resolver) => {
        let listo = false;
        const contestar = (ok) => {
          if (listo) return;
          listo = true;
          document.removeEventListener('cortante:imagen', alLlegar);
          resolver(ok);
        };
        const alLlegar = (e) => {
          if (e.detail.motivo === 'carga') contestar(Boolean(e.detail.ok));
        };
        document.addEventListener('cortante:imagen', alLlegar);
        setTimeout(() => contestar(false), MS_ESPERA_FOTO);
        // El despacho va directo y no por `this.anunciar`: dentro de un objeto
        // literal `this` es el objeto devuelto, asi que desestructurar
        // (`const {cargar} = previa`) lo dejaria en undefined. Un metodo que
        // solo funciona si nadie lo saca de su objeto es una trampa puesta.
        document.dispatchEvent(new CustomEvent('cortante:listo', { detail: { url } }));
      });
    },

    /** Rinde la foto de lo que hay en el visor y la sube a `destino`. */
    exportar(destino) {
      return pedirFoto(destino);
    },
  };
}

/**
 * Una paleta de color de la vista previa.
 *
 * Cambia lo que se ve en pantalla y nada mas: el `.3mf` y el `.glb` salen
 * con los materiales del motor, y al imprimir el color lo pone el filamento.
 * Por eso no reactiva el boton de generar ni se manda al servidor. Se
 * recuerda en `localStorage`, igual que el tema.
 *
 * La lista de colores la dibuja el template desde `COLORES` del router: aca
 * no hay ningun codigo de color escrito, solo el que trae cada boton. Las dos
 * pantallas con visor reciben la MISMA tupla, importada y no copiada.
 *
 * **Recibe VARIOS contenedores y sincroniza por valor, no por elemento.** El
 * color de la pieza se ofrece en dos lugares —la paleta que flota sobre el
 * visor 3D y la de la barra de la vista imagen— y es un solo valor: apretar
 * una muestra tiene que marcar la del mismo color en las dos. Comparar por
 * `dataset.color` en vez de por identidad del boton es todo lo que hace
 * falta para eso, y es lo que evita tener que mantener dos paletas en
 * sincronia a mano.
 */
function iniciarPaleta({ selectores, clave, evento }) {
  const muestras = selectores.flatMap((sel) => $$(`${sel} .paleta__color`));
  if (!muestras.length) return;

  let guardado = null;
  try {
    guardado = localStorage.getItem(clave);
  } catch (_) {
    /* sin localStorage se arranca con el default, que lo marca el template */
  }

  const aplicar = (color, recordar) => {
    muestras.forEach((o) => o.setAttribute('aria-pressed', String(o.dataset.color === color)));
    // `sinPiso` viaja junto al color y NO se deduce del hex: la muestra lo
    // trae del router (`COLORES_FONDO`), y leerlo del DOM es lo que evita
    // que el front tenga su propia idea de cual fondo no lleva piso. En las
    // paletas de la pieza ninguna muestra lo declara y siempre sale `false`.
    const elegida = muestras.find((o) => o.dataset.color === color);
    const sinPiso = Boolean(elegida) && elegida.dataset.sinPiso === '1';
    document.dispatchEvent(new CustomEvent(evento, { detail: { color, sinPiso } }));
    if (!recordar) return;
    try {
      localStorage.setItem(clave, color);
    } catch (_) {
      /* sin persistencia: el color vale para esta pagina igual */
    }
  };

  muestras.forEach((b) => b.addEventListener('click', () => aplicar(b.dataset.color, true)));

  // Un color guardado que ya no este en `COLORES` se descarta: la paleta del
  // servidor manda, y si cambio la lista el valor viejo no existe mas.
  //
  // Sin nada guardado se aplica la PRIMERA muestra, que es la que el macro
  // `paleta` marca por default. Antes esto era un parametro
  // (`indiceDefecto`) para que el fondo pudiera arrancar en la segunda, y
  // eso creaba dos fuentes de verdad para el mismo dato: el `activo` del
  // template y el indice de aca. Cuando este ciclo movio el del template y
  // no el de aca, el HTML servido marcaba blanco y la pantalla mostraba
  // gris — y ningun test lo vio, porque ninguno ejecuta JS. Con un solo
  // lugar donde se declara, esa desincronizacion no se puede volver a dar.
  const disponibles = muestras.map((b) => b.dataset.color);
  const inicial = disponibles.includes(guardado) ? guardado : disponibles[0];
  aplicar(inicial, false);
}

function iniciarCortante() {
  const estado = $('#estado');
  const generar = $('#generar');
  const pistaGenerar = $('#pista-generar');
  const botonesModo = $$('.segmentado button[data-modo]');

  const recordado = leerEstado('cortante');
  let archivo = null;
  let modo = recordado.modo || 'cortante+marcador';
  let generando = false;
  let firmaGenerada = null;
  const origenDeUrl = new URLSearchParams(location.search).get('origen');
  let origen = origenDeUrl || recordado.origen || null;
  let trabajoId = trabajoQueSigueVigente(recordado, origenDeUrl);
  // La vista elegida la administra `iniciarVistaPrevia3D`; aca se guarda solo
  // para `recordar()`, que persiste el estado de ESTA pantalla.
  let vista = recordado.vista === 'imagen' ? 'imagen' : '3d';

  const entradas = () => $$('.parametros .campo[data-campo] input');

  const recordar = () =>
    guardarEstado('cortante', {
      origen,
      trabajo: trabajoId,
      modo,
      vista,
      campos: Object.fromEntries(entradas().map((i) => [i.name, i.value])),
    });

  // Los 10 parametros y el modo vuelven tal como quedaron. El ARCHIVO no puede
  // volver —un `File` no es serializable y el navegador no deja reconstruirlo—,
  // asi que lo que se recupera es el trabajo que ya esta en el servidor. Restaurar antes de enganchar los listeners: asignar `.value`
  // desde el codigo no dispara `input`, y no hay que guardar lo que se acaba
  // de leer.
  if (recordado.campos) {
    entradas().forEach((i) => {
      if (recordado.campos[i.name] !== undefined) i.value = recordado.campos[i.name];
    });
  }
  botonesModo.forEach((o) => o.setAttribute('aria-pressed', String(o.dataset.modo === modo)));

  botonesModo.forEach((b) =>
    b.addEventListener('click', () => {
      modo = b.dataset.modo;
      botonesModo.forEach((o) => o.setAttribute('aria-pressed', String(o === b)));
      aplicarModo();
      repasarGenerar();
      recordar();
    })
  );
  aplicarModo();

  /**
   * En modo `cortante` el motor ignora los parametros del marcador.
   *
   * La pantalla los APAGA en vez de esconderlos: tampoco se envian —un valor
   * que no cambia nada no puede llegar a rechazar el pedido por estar fuera de
   * rango—, pero que sigan a la vista es lo que muestra que el modo hace algo.
   * Escondidos, cambiar de modo era un panel que desaparecia sin explicacion.
   */
  function aplicarModo() {
    const conMarcador = modo === 'cortante+marcador';
    $('#panel-marcador').classList.toggle('panel--inhabilitado', !conMarcador);
    $$('#panel-marcador input').forEach((i) => {
      i.disabled = !conMarcador;
    });
    const chip = $('#chip-marcador');
    chip.className = `chip ${conMarcador ? 'chip--ok' : 'chip--proceso'}`;
    texto(chip, conMarcador ? 'incluido' : 'sin marcador');
    mostrar($('#nota-modo'), !conMarcador);
  }

  const camposActivos = () =>
    $$('.parametros .campo[data-campo]').filter(
      (c) => modo === 'cortante+marcador' || !c.dataset.soloMarcador
    );

  /**
   * Todo lo que define el archivo que va a salir, en un solo string.
   *
   * Es una firma y no un flag de "toque algo" a proposito: asi cambiar un
   * valor y volverlo atras vuelve a apagar el boton, que es la verdad — ese
   * archivo ya esta hecho. El color del visor **no entra**: no viaja al
   * archivo, y regenerar por un color seria cobrarle al usuario cinco
   * segundos por nada.
   */
  const firma = () =>
    JSON.stringify([
      modo,
      archivo ? [archivo.name, archivo.size, archivo.lastModified] : origen,
      camposActivos().map((c) => $('input', c).value),
    ]);

  /** El unico lugar que prende y apaga el boton de generar. */
  function repasarGenerar() {
    const alDia = firmaGenerada !== null && firma() === firmaGenerada;
    generar.disabled = generando || !(archivo || origen) || alDia;
    mostrar(pistaGenerar, alDia && !generando);
  }

  $$('.parametros input').forEach((i) =>
    i.addEventListener('input', () => {
      repasarGenerar();
      recordar();
    })
  );
  $('#restaurar').addEventListener('click', () => {
    $$('.parametros input').forEach((i) => {
      i.value = i.dataset.defecto;
    });
    limpiarErroresDeCampo();
    // Asignar `.value` desde el codigo no dispara `input`: hay que repasar.
    repasarGenerar();
    recordar();
  });

  conectarZona(
    $('#zona'),
    $('#entrada'),
    (archivos) => {
      archivo = archivos[0];
      origen = null; // un archivo del disco pisa al que venia encadenado
      texto($('#nombre-archivo'), archivo.name);
      mostrar($('#chip-archivo'), true);
      repasarGenerar();
      recordar();
    },
    () => {
      archivo = null;
      origen = null;
      firmaGenerada = null; // sin archivo no hay nada "al dia" que respetar
      mostrar($('#chip-archivo'), false);
      repasarGenerar();
      recordar();
    }
  );

  if (origen) restaurarOrigen(origen);
  if (trabajoId) retomarTrabajo(trabajoId);

  async function restaurarOrigen(id) {
    const nombre = await nombreDeTrabajo(id, 'viene de la pantalla anterior');
    if (nombre === null) {
      origen = null;
      repasarGenerar();
      recordar();
      avisar(estado, 'alerta', 'El archivo de la pantalla anterior ya no esta disponible.');
      return;
    }
    texto($('#nombre-archivo'), nombre);
    mostrar($('#chip-archivo'), true);
    repasarGenerar();
    // Persistir ACA y no solo al tocar algo: si el usuario se va por el menu
    // y vuelve, lo recordado tiene que ser este origen y no el del diseño
    // anterior, o `trabajoQueSigueVigente` lo daria por vigente.
    recordar();
  }

  /** Vuelve a enganchar un trabajo que quedo corriendo al cambiar de seccion. */
  async function retomarTrabajo(id) {
    try {
      const t = await pedirJson(`/api/trabajos/${encodeURIComponent(id)}`);
      if (t.estado === 'error') return;
      const listo =
        t.estado === 'listo'
          ? t
          : siFalloTirar(await sondear(id, (x) => avisar(estado, 'info', `${x.etapa}…`)));
      pintarResultado(listo);
      firmaGenerada = firma();
      repasarGenerar();
    } catch (_) {
      trabajoId = null; // vencio por TTL: se olvida sin molestar al usuario
      recordar();
    }
  }


  // Las paletas, las dos vistas y toda la coreografia de la foto viven en el
  // modulo compartido: esta pantalla y la de post tienen que comportarse igual.
  const previa = iniciarVistaPrevia3D({
    obtenerTrabajoId: () => trabajoId,
    vistaInicial: vista,
    alCambiarVista: (elegida) => {
      vista = elegida;
      recordar();
    },
  });

  generar.addEventListener('click', async () => {
    limpiarErroresDeCampo();
    generando = true;
    repasarGenerar();
    const datos = new FormData();
    if (archivo) datos.append('archivo', archivo, archivo.name);
    else if (origen) datos.append('origen', origen);
    datos.append('modo', modo);
    camposActivos().forEach((c) => {
      const i = $('input', c);
      datos.append(i.name, i.value);
    });
    // La firma se saca ANTES de mandar: es la de los valores que se enviaron.
    // Si el usuario toca un campo mientras el motor trabaja, lo que vuelve no
    // corresponde a lo que hay en pantalla y el boton tiene que quedar vivo.
    const firmaEnviada = firma();

    try {
      avisar(estado, 'info', 'Enviando…');
      const lanzado = await enviar('/api/cortante', datos);
      trabajoId = lanzado.id;
      recordar();
      const trabajo = siFalloTirar(
        await sondear(lanzado.id, (t) => avisar(estado, 'info', `${t.etapa}…`))
      );
      pintarResultado(trabajo);
      firmaGenerada = firmaEnviada;
    } catch (e) {
      marcarCampo(e);
      avisar(estado, 'error', e.message);
      firmaGenerada = null;
    } finally {
      generando = false;
      repasarGenerar();
    }
  });

  function limpiarErroresDeCampo() {
    $$('.campo[data-campo]').forEach((c) => {
      c.classList.remove('campo--error');
      mostrar($('[data-rol="error"]', c), false);
    });
  }

  /** Un 422 del motor trae el nombre del parametro: se pinta ese campo. */
  function marcarCampo(e) {
    const nombre = e && e.detalle && e.detalle.parametro;
    if (!nombre) return;
    const campo = $(`.campo[data-campo="${nombre}"]`);
    if (!campo) return;
    campo.classList.add('campo--error');
    const marca = $('[data-rol="error"]', campo);
    texto($('span', marca), e.detalle.limite || 'valor fuera de rango');
    mostrar(marca, true);
    campo.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  /**
   * El trabajo termino: descargas habilitadas y reporte a la vista.
   *
   * Los enlaces se habilitan aca y no cuando la vista previa termina de
   * pintar. Antes esperaban al visor —la idea era que se viera la pieza antes
   * de bajarla—, pero el archivo ya esta completo en el servidor y el visor
   * puede fallar por cosas que no dicen nada de el: un navegador sin WebGL, un
   * GLB que no carga. Lo unico que sigue dependiendo del preview es la PISTA
   * de abajo, que es texto y no una traba.
   */
  function pintarResultado(trabajo) {
    mostrar(estado, false);
    texto($('#pista-visor'), 'geometria real del archivo que vas a descargar');

    // ⚠ Antes de pintar, no despues: ver el docstring de `reiniciar`.
    previa.reiniciar();

    $$('#botones-descarga a[data-clave]').forEach((a) => {
      // La foto es el unico archivo que TODAVIA no esta en el servidor cuando
      // el trabajo termina: la rinde el navegador y se sube al pedirla. Asi
      // que su entrada no se decide por `trabajo.archivos` sino por si la
      // vista imagen puede producirla — y eso lo dice `cortante:imagen`.
      const disponible =
        a.dataset.clave === CLAVE_FOTO
          ? previa.hayFoto
          : trabajo.archivos.includes(a.dataset.clave);
      // El `href` se quita cuando el archivo NO esta: si la generacion anterior
      // dejo un marcador y esta no, el enlace viejo apuntaria a otro trabajo.
      if (disponible) a.href = urlArchivo(trabajo.id, a.dataset.clave);
      else a.removeAttribute('href');
      mostrar(a, disponible);
    });
    texto($('#pista-descargas'), PISTA_LISTA);
    mostrar($('#panel-descargas'), true);
    mostrar($('#bajar-todo'), true);

    if (trabajo.archivos.includes('glb')) {
      previa.anunciar(urlArchivo(trabajo.id, 'glb'));
    }
    pintarReporte(trabajo.reporte);
  }
}

/* ── F4: las fotos de cortantes que ya existen ───────────────────────────── */

/**
 * De que es cada archivo, deducido de como lo nombro el motor al exportarlo.
 *
 * `export.py` escribe `<base>_cortador.stl` y `<base>_marcador.stl`, asi que el
 * sufijo es la unica pista de que dos archivos son **un solo diseño**. Se acepta
 * tambien el guion medio porque los sistemas de archivos y las descargas del
 * navegador lo meten solos al desduplicar.
 *
 * ⚠ **Esto corre en el navegador y no en el servidor, y no es un detalle.** El
 * servidor no mira ni un nombre de archivo del cliente para decidir nada (ver
 * `app/archivos.py`): lo que le llega es una lista de enteros y de roles, ya
 * resuelta. Aca es seguro porque un error de emparejado no es un problema de
 * seguridad sino de comodidad, y ademas se puede corregir a mano.
 */
const ROL_POR_SUFIJO = [
  [/[_-]cortador$/i, 'cortador'],
  [/[_-]marcador$/i, 'marcador'],
];

/** `kitty_cortador.stl` -> `{ base: 'kitty', rol: 'cortador' }`. */
function leerNombre(nombre) {
  const stem = String(nombre || '').replace(/\.[^.]+$/, '');
  for (const [patron, rol] of ROL_POR_SUFIJO) {
    if (patron.test(stem)) return { base: stem.replace(patron, ''), rol };
  }
  return { base: stem, rol: 'unico' };
}

/**
 * Agrupa los archivos en diseños. El cortador y su marcador van juntos.
 *
 * Empareja por base **y** por rol complementario: dos cortadores de la misma
 * base no son un diseño, son dos corridas del mismo dibujo. Lo que no encuentra
 * pareja queda como diseño de un archivo, que es el caso del `.3mf` combinado.
 */
function agruparArchivos(archivos) {
  const leidos = archivos.map((a, i) => ({ i, archivo: a, ...leerNombre(a.name) }));
  const disenos = [];
  const usados = new Set();

  leidos.forEach((uno) => {
    if (usados.has(uno.i) || uno.rol === 'unico') return;
    const buscado = uno.rol === 'cortador' ? 'marcador' : 'cortador';
    const par = leidos.find(
      (otro) => !usados.has(otro.i) && otro.i !== uno.i && otro.base === uno.base && otro.rol === buscado
    );
    if (!par) return;
    usados.add(uno.i);
    usados.add(par.i);
    // El cortador primero, siempre: que el orden de la lista no dependa del
    // orden en que el sistema de archivos devolvio los nombres.
    const [cortador, marcador] = uno.rol === 'cortador' ? [uno, par] : [par, uno];
    disenos.push({ base: uno.base, partes: [cortador, marcador] });
  });

  leidos.forEach((uno) => {
    if (usados.has(uno.i)) return;
    usados.add(uno.i);
    disenos.push({ base: uno.base, partes: [uno] });
  });

  // En el orden en que los eligio el usuario: el set se arma con este orden y
  // reordenarlo por el emparejado seria mover piezas que nadie movio.
  disenos.sort((a, b) => Math.min(...a.partes.map((p) => p.i)) - Math.min(...b.partes.map((p) => p.i)));
  return disenos;
}

/**
 * Sube hasta 25 diseños y les saca a todos la MISMA foto que la pantalla de
 * cortante, mas una lamina con el set entero.
 *
 * Lo que hace propio de esta pantalla es poco a proposito: agrupar los archivos,
 * y recorrer los diseños de a uno pidiendole la foto al visor. Todo lo que hace
 * que la foto salga igual —el estudio de luces, el encuadre, las paletas, el
 * handshake de exportacion— vive en `iniciarVistaPrevia3D` y en `preview3d.js`,
 * compartidos con `iniciarCortante`.
 */
function iniciarPost() {
  const estado = $('#estado');
  const procesar = $('#procesar');
  const lista = $('#lista-disenos');

  const recordado = leerEstado('post');
  let disenos = [];
  let trabajoId = recordado.trabajo || null;
  let vista = recordado.vista === 'imagen' ? 'imagen' : '3d';
  let mirando = 1;
  let trabajando = false;

  const recordar = () => guardarEstado('post', { trabajo: trabajoId, vista });

  const urlGlb = (n) => `/api/trabajos/${encodeURIComponent(trabajoId)}/diseno/${n}/archivo/glb`;
  const urlVista = (n) =>
    `/api/trabajos/${encodeURIComponent(trabajoId)}/diseno/${n}/archivo/jpg_vista`;
  const urlSubida = (n) => `/api/trabajos/${encodeURIComponent(trabajoId)}/diseno/${n}/imagen`;

  const previa = iniciarVistaPrevia3D({
    obtenerTrabajoId: () => trabajoId,
    vistaInicial: vista,
    alCambiarVista: (elegida) => {
      vista = elegida;
      recordar();
    },
    urlFoto: () => urlSubida(mirando),
    // Las fotos ya se subieron todas al armar el lote. Volver a rendir al bajar
    // subiria la del diseño que quedo en el visor encima de la del que se pidio.
    subirAlDescargar: false,
  });

  /* ── Elegir archivos y armar los diseños ───────────────────────────────── */

  conectarZona(
    $('#zona'),
    $('#entrada'),
    (archivos) => {
      disenos = agruparArchivos(Array.from(archivos));
      pintarLista();
      recordar();
    },
    () => {
      disenos = [];
      pintarLista();
    }
  );

  /**
   * La lista de diseños, con los botones para corregir el emparejado.
   *
   * Se repinta entera en cada cambio. Con 25 filas eso no cuesta nada y evita
   * el estado a medias de actualizar una fila sola: los indices que se muestran
   * son posicionales, asi que separar o unir los corre a todos.
   */
  function pintarLista() {
    lista.replaceChildren();
    disenos.forEach((d, i) => lista.appendChild(filaDeDiseno(d, i)));
    mostrar($('#panel-disenos'), disenos.length > 0);
    texto($('#cuenta-disenos'), textoDeCuenta());
    const exceso = disenos.length > MAX_DISENOS;
    mostrar($('#aviso-exceso'), exceso);
    procesar.disabled = trabajando || disenos.length === 0 || exceso;
  }

  function textoDeCuenta() {
    const archivos = disenos.reduce((n, d) => n + d.partes.length, 0);
    const plural = (n, uno, varios) => `${n} ${n === 1 ? uno : varios}`;
    return `${plural(disenos.length, 'diseño', 'diseños')} · ${plural(archivos, 'archivo', 'archivos')}`;
  }

  function filaDeDiseno(d, i) {
    const fila = document.createElement('div');
    fila.className = 'diseno';

    const cuerpo = document.createElement('div');
    cuerpo.className = 'diseno__cuerpo';
    const titulo = document.createElement('div');
    titulo.className = 'diseno__nombre';
    titulo.textContent = `${i + 1}. ${d.base || 'sin nombre'}`;
    const detalle = document.createElement('div');
    detalle.className = 'lista__meta';
    detalle.textContent = d.partes.map((p) => `${p.archivo.name} · ${p.rol}`).join('  +  ');
    cuerpo.append(titulo, detalle);

    const acciones = document.createElement('div');
    acciones.className = 'diseno__acciones';
    if (d.partes.length === 2) {
      acciones.appendChild(
        boton('Separar', 'Tratarlos como dos diseños distintos', () => separar(i))
      );
    } else if (disenos[i + 1] && disenos[i + 1].partes.length === 1) {
      acciones.appendChild(
        boton('Unir', 'Unir con el diseño de abajo, como cortante + marcador', () => unir(i))
      );
    }
    fila.append(cuerpo, acciones);
    return fila;
  }

  function boton(etiqueta, titulo, alHacer) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'boton boton--fantasma boton--chico';
    b.textContent = etiqueta;
    b.title = titulo;
    b.addEventListener('click', alHacer);
    return b;
  }

  /** Parte un diseño de dos archivos en dos diseños de uno. */
  function separar(i) {
    const partes = disenos[i].partes.map((p) => ({ ...p, rol: 'unico' }));
    disenos.splice(i, 1, ...partes.map((p) => ({ base: p.base, partes: [p] })));
    pintarLista();
  }

  /**
   * Une un diseño con el siguiente. El primero es el cortador.
   *
   * El orden importa poco —el color de cada cuerpo lo pisa la paleta— pero el
   * servidor exige que un par sea cortador + marcador, asi que hay que decidir.
   * Si los nombres lo dicen, mandan ellos; si no, el de arriba es el cortador,
   * que es el caso normal de un par exportado en ese orden.
   */
  function unir(i) {
    const [uno, otro] = [disenos[i].partes[0], disenos[i + 1].partes[0]];
    const leidoA = leerNombre(uno.archivo.name);
    const leidoB = leerNombre(otro.archivo.name);
    const invertido = leidoA.rol === 'marcador' || leidoB.rol === 'cortador';
    const [cortador, marcador] = invertido ? [otro, uno] : [uno, otro];
    disenos.splice(i, 2, {
      base: leerNombre(cortador.archivo.name).base,
      partes: [
        { ...cortador, rol: 'cortador' },
        { ...marcador, rol: 'marcador' },
      ],
    });
    pintarLista();
  }

  /* ── Mandar el lote y sacar las fotos ──────────────────────────────────── */

  procesar.addEventListener('click', async () => {
    trabajando = true;
    procesar.disabled = true;
    mostrar($('#panel-descargas'), false);
    mostrar($('#panel-set'), false);
    try {
      const trabajo = await enviarLote();
      await sacarLasFotos(trabajo);
    } catch (e) {
      avisar(estado, 'error', e.message);
    } finally {
      trabajando = false;
      pintarLista();
    }
  });

  /**
   * Sube los archivos con la agrupacion resuelta y espera a que terminen.
   *
   * Los archivos van en el orden de los diseños y la agrupacion se escribe sobre
   * ESE orden, no sobre el que tenian al elegirlos: separar y unir reordenan la
   * lista, y mandar dos ordenes distintos es como se arma un diseño con las
   * piezas de otro.
   */
  async function enviarLote() {
    const datos = new FormData();
    const grupos = [];
    let n = 0;
    disenos.forEach((d) => {
      const partes = d.partes.map((p) => {
        datos.append('archivo', p.archivo, p.archivo.name);
        return `${n++}:${p.rol}`;
      });
      grupos.push(partes.join(','));
    });
    datos.append('agrupacion', grupos.join(';'));
    // Como se va a llamar la descarga de cada diseño. En un par los dos archivos
    // comparten base, y esa base es el nombre del diseño — no el de un archivo.
    datos.append('nombres', disenos.map((d) => d.base).join(';'));

    avisar(estado, 'info', 'Enviando…');
    const lanzado = await enviar('/api/post', datos);
    trabajoId = lanzado.id;
    recordar();
    return siFalloTirar(
      await sondear(lanzado.id, (t) => avisar(estado, 'info', `${t.etapa}…`))
    );
  }

  /**
   * Recorre los diseños de a uno: cargar el modelo, rendir la foto, subirla.
   *
   * **En serie y no en paralelo**, igual que la conversion del lado del
   * servidor, pero por otro motivo: hay UN solo visor y UN solo canvas, asi que
   * sacar dos fotos a la vez es sacar dos veces la misma. `previa.cargar` espera
   * a que el modelo este puesto antes de disparar la foto — sin esa espera, la
   * foto del diseño 2 seria la del 1.
   */
  async function sacarLasFotos(trabajo) {
    const ok = [];
    const sin = [];
    for (const d of (trabajo.reporte && trabajo.reporte.disenos) || []) {
      if (!d.ok) {
        sin.push(d);
        continue;
      }
      mirando = d.indice;
      avisar(estado, 'info', `Sacando la foto ${ok.length + 1} de ${trabajo.disenos}…`);
      if (!(await previa.cargar(urlGlb(d.indice)))) {
        sin.push({ ...d, error: { mensaje: 'no se pudo cargar el modelo' } });
        continue;
      }
      const r = await previa.exportar(urlSubida(d.indice));
      if (r.ok) ok.push(d);
      else sin.push({ ...d, error: { mensaje: r.error } });
    }

    pintarDescargas(trabajo, ok, sin);
    if (ok.length) await armarSet();
    mostrar(estado, false);
  }

  /** Le pide al servidor que pegue las fotos en una sola imagen. */
  async function armarSet() {
    avisar(estado, 'info', 'Armando el set…');
    try {
      const r = await pedirJson(`/api/trabajos/${encodeURIComponent(trabajoId)}/set`, {
        method: 'POST',
      });
      $('#img-set').src = `/api/trabajos/${encodeURIComponent(trabajoId)}/archivo/set?t=${Date.now()}`;
      $('#bajar-set').href = `/api/trabajos/${encodeURIComponent(trabajoId)}/archivo/set`;
      // El reparto tal cual quedo (`3-2-2`) y no "3x3": con filas desparejas
      // el par columnas x filas no dice como quedo armado.
      texto(
        $('#pista-set'),
        `${r.celdas} diseños · ${r.distribucion.join('-')} · ${r.tamano_px.join('×')} px`
      );
      mostrar($('#panel-set'), true);
    } catch (e) {
      // El set es lo ultimo: que falle no invalida las fotos sueltas, que ya
      // estan arriba y se pueden bajar. Se dice y se sigue.
      texto($('#pista-set'), `No se pudo armar el set: ${e.message}`);
      mostrar($('#panel-set'), true);
    }
  }

  /** Una entrada de descarga por diseño, mas el ZIP. Lo que fallo se declara. */
  function pintarDescargas(trabajo, ok, sin) {
    const grupo = $('#botones-descarga');
    grupo.replaceChildren();
    ok.forEach((d) => {
      const a = document.createElement('a');
      a.className = 'boton boton--secundario';
      a.dataset.clave = 'jpg_vista';
      a.download = '';
      a.href = urlVista(d.indice);
      a.textContent = `Foto ${d.indice}`;
      a.addEventListener('click', () => {
        // Mirar el que se baja: el visor queda en el ultimo del lote y ver una
        // pieza mientras se baja otra es exactamente lo que confunde.
        mirando = d.indice;
        previa.cargar(urlGlb(d.indice));
      });
      grupo.appendChild(a);
    });
    const zip = document.createElement('a');
    zip.className = 'boton boton--todo';
    zip.href = urlZip(trabajoId);
    zip.download = '';
    zip.textContent = `Descargar todo (ZIP)`;
    grupo.appendChild(zip);

    texto(
      $('#pista-descargas'),
      sin.length
        ? `${ok.length} de ${trabajo.disenos} listos. ${sin.length} no se pudieron: ${sin
            .map((d) => `#${d.indice} (${(d.error && d.error.mensaje) || 'error'})`)
            .join(', ')}.`
        : PISTA_LISTA
    );
    mostrar($('#panel-descargas'), true);
    texto($('#pista-visor'), 'la geometria de los archivos que subiste');
  }
}

/* ── Reporte de fidelidad ────────────────────────────────────────────────── */

const VEREDICTOS = {
  limpio: ['VERIFICADO', 'La geometria entregada coincide con el arte original.'],
  alerta: ['VERIFICADO CON ADVERTENCIAS', 'Se cumple lo verificable, pero hay puntos a mirar.'],
  error: ['NO VERIFICADO', 'Alguna comprobacion no cerro: revisalo antes de imprimir.'],
};

/** Los tres estados salen de datos publicos del reporte, no de un campo aparte. */
function claseDeVeredicto(r) {
  if (!r || !r.todo_ok) return 'error';
  return (r.advertencias || []).length ? 'alerta' : 'limpio';
}

function pintarReporte(r) {
  const panel = $('#panel-reporte');
  if (!panel || !r) return;

  const clase = claseDeVeredicto(r);
  const [titulo, bajada] = VEREDICTOS[clase];
  $('#veredicto').className = `veredicto veredicto--${clase}`;
  texto($('#veredicto-titulo'), titulo);
  texto($('#veredicto-bajada'), bajada);

  texto($('#r-contornos'), `${r.contornos_original ?? '—'} / ${r.contornos_final ?? '—'}`);
  texto($('#r-huecos'), `${r.huecos_original ?? '—'} / ${r.huecos_final ?? '—'}`);
  texto($('#r-area'), r.area_diferencia_mm2 == null ? '—' : `${r.area_diferencia_mm2.toFixed(3)} mm²`);
  texto(
    $('#r-desvio'),
    [r.desvio_p50_mm, r.desvio_p99_mm, r.desvio_max_mm]
      .map((v) => (v == null ? '—' : v.toFixed(3)))
      .join(' / ')
  );
  texto($('#r-dilatacion'), mm(r.dilatacion_aplicada_mm, 3));

  const trazo = r.trazo || {};
  texto($('#r-trazo'), `${mm(r.mediana_trazo_antes_mm, 2)} → ${mm(trazo.mediana, 2)}`);
  texto($('#r-hueco-min'), mm((r.huecos || {}).minimo, 2));
  texto($('#r-muescas'), `${mm(r.muescas_selladas_mm, 2)} (${(r.muescas_selladas_pct || 0).toFixed(1)} %)`);
  texto($('#r-luz'), `${mm(r.luz_minima_real_mm, 2)} / ${mm(r.luz_nominal_mm, 2)}`);
  texto($('#r-lado'), mm(r.lado_mayor_final_mm, 2));

  pintarFilas($('#r-topologias'), r.topologias || [], (t) => [
    t.objeto,
    `${t.watertight ? 'cerrado' : 'ABIERTO'} · Euler ${t.euler}/${t.euler_esperado}`,
    t.ok,
  ]);
  pintarFilas($('#r-secciones'), r.secciones || [], (s) => [
    `z = ${s.z_mm} mm`,
    `${(s.desvio_relativo * 100).toFixed(2)} % de desvio`,
    s.ok,
  ]);

  pintarAdvertencias(r.advertencias || []);
  mostrar(panel, true);
}

function pintarFilas(destino, items, mapear) {
  if (!destino) return;
  destino.replaceChildren();
  items.forEach((item) => {
    const [etiqueta, valor, ok] = mapear(item);
    const fila = document.createElement('div');
    fila.className = 'dato';
    const izq = document.createElement('span');
    izq.textContent = etiqueta;
    const der = document.createElement('span');
    der.className = `chip ${ok ? 'chip--ok' : 'chip--error'}`;
    der.style.fontSize = '10.5px';
    der.textContent = valor;
    fila.append(izq, der);
    destino.appendChild(fila);
  });
}

function pintarAdvertencias(lista) {
  const panel = $('#panel-advertencias');
  if (!panel) return;
  panel.replaceChildren();
  lista.forEach((a) => {
    const caja = document.createElement('div');
    caja.className = 'aviso aviso--alerta';
    caja.textContent = a;
    panel.appendChild(caja);
  });
  mostrar(panel, lista.length > 0);
}

/* ── Arranque ────────────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  iniciarTema();
  const pagina = document.body.dataset.pagina;
  if (pagina === 'conversor') iniciarConversor();
  if (pagina === 'lineas') iniciarLineas();
  if (pagina === 'cortante') iniciarCortante();
  if (pagina === 'post') iniciarPost();
});
