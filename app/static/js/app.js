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

/* ── Utilidades ──────────────────────────────────────────────────────────── */

const $ = (sel, raiz = document) => raiz.querySelector(sel);
const $$ = (sel, raiz = document) => Array.from(raiz.querySelectorAll(sel));
const esperar = (ms) => new Promise((r) => setTimeout(r, ms));

const MS_POLLING_RAPIDO = 800;
const MS_POLLING_LENTO = 2000;
const MS_HASTA_LENTO = 30000;
const MS_LIMITE = 150000;
const MS_ESPERA_PREVIEW = 15000;

const PISTA_SIN_PREVIEW =
  'No se pudo mostrar la vista previa. Los archivos estan completos igual.';

// "La geometria" y no "lo que estas viendo": el color de la pieza lo elige la
// paleta del visor y no viaja al archivo. La forma si es exactamente esta.
const PISTA_CON_PREVIEW = 'Lo que estas viendo es exactamente la geometria que se descarga.';

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

async function pedirJson(url) {
  return comoJson(await fetch(url, { headers: { Accept: 'application/json' } }));
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

function conectarZona(zona, input, alElegir) {
  if (!zona || !input) return;
  const boton = $('#elegir', zona) || $('#elegir');
  if (boton) boton.addEventListener('click', () => input.click());
  zona.addEventListener('click', (e) => {
    if (e.target === zona) input.click();
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

/* ── F2: correccion de lineas ────────────────────────────────────────────── */

function iniciarLineas() {
  const estado = $('#estado');
  const procesar = $('#procesar');
  const swMacizos = $('#switch-macizos');
  let archivo = null;
  const origen = new URLSearchParams(location.search).get('origen');

  $('#boton-macizos').addEventListener('click', () => {
    const activo = swMacizos.getAttribute('aria-checked') === 'true';
    swMacizos.setAttribute('aria-checked', String(!activo));
  });

  conectarZona($('#zona'), $('#entrada'), (archivos) => {
    archivo = archivos[0];
    texto($('#nombre-archivo'), archivo.name);
    mostrar($('#chip-archivo'), true);
    $('#img-original').src = URL.createObjectURL(archivo);
    mostrar($('#panel-comparador'), true);
    procesar.disabled = false;
  });

  if (origen) {
    $('#img-original').src = urlArchivo(origen, 'jpg');
    texto($('#nombre-archivo'), 'viene del conversor');
    mostrar($('#chip-archivo'), true);
    mostrar($('#panel-comparador'), true);
    procesar.disabled = false;
  }

  procesar.addEventListener('click', async () => {
    procesar.disabled = true;
    const datos = new FormData();
    if (archivo) datos.append('archivo', archivo, archivo.name);
    else if (origen) datos.append('origen', origen);
    datos.append('contornear_macizos', swMacizos.getAttribute('aria-checked'));

    try {
      avisar(estado, 'info', 'Procesando la imagen…');
      const lanzado = await enviar('/api/lineas', datos);
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
    $('#img-corregida').src = `${urlArchivo(trabajo.id, 'png')}?v=${Date.now()}`;
    const r = trabajo.reporte || {};
    texto($('#d-umbral'), String(r.umbral_usado ?? '—'));
    texto($('#d-ancho'), `${r.ancho_trazo_px ?? '—'} px`);
    texto($('#d-zonas'), String(r.zonas_contorneadas ?? 0));
    texto($('#d-area'), `${(r.area_contorneada_px ?? 0).toLocaleString('es-AR')} px`);
    mostrar($('#panel-declaracion'), true);

    if (r.zonas_contorneadas > 0) {
      texto(
        $('#texto-macizos'),
        `Se reemplazaron ${r.zonas_contorneadas} zona(s) maciza(s) por su contorno, ` +
          `afectando ${(r.area_contorneada_px ?? 0).toLocaleString('es-AR')} px. ` +
          'Es la unica modificacion del arte que se aplico.'
      );
      mostrar($('#aviso-macizos'), true);
    }

    $('#bajar').href = urlArchivo(trabajo.id, 'png');
    $('#bajar-svg').href = urlArchivo(trabajo.id, 'svg');
    $('#seguir').href = `/cortante?origen=${encodeURIComponent(trabajo.id)}`;
    mostrar($('#pie'), true);
  }
}

/* ── F3: crear cortante ──────────────────────────────────────────────────── */

function iniciarCortante() {
  const estado = $('#estado');
  const generar = $('#generar');
  const pistaGenerar = $('#pista-generar');
  const botonesModo = $$('.segmentado button[data-modo]');
  let archivo = null;
  let modo = 'cortante+marcador';
  let esperaPreview = null;
  let generando = false;
  let firmaGenerada = null;
  const origen = new URLSearchParams(location.search).get('origen');

  botonesModo.forEach((b) =>
    b.addEventListener('click', () => {
      modo = b.dataset.modo;
      botonesModo.forEach((o) => o.setAttribute('aria-pressed', String(o === b)));
      aplicarModo();
      repasarGenerar();
    })
  );
  aplicarModo();

  /**
   * En modo `cortante` el motor ignora los parametros del marcador, asi que
   * la pantalla los saca de encima. No se envian tampoco: un valor que no
   * cambia nada no puede llegar a rechazar el pedido por estar fuera de rango.
   */
  function aplicarModo() {
    const conMarcador = modo === 'cortante+marcador';
    $$('.campo[data-solo-marcador]').forEach((c) => mostrar(c, conMarcador));
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
      $('#con-stl').checked,
      archivo ? [archivo.name, archivo.size, archivo.lastModified] : origen,
      camposActivos().map((c) => $('input', c).value),
    ]);

  /** El unico lugar que prende y apaga el boton de generar. */
  function repasarGenerar() {
    const alDia = firmaGenerada !== null && firma() === firmaGenerada;
    generar.disabled = generando || !(archivo || origen) || alDia;
    mostrar(pistaGenerar, alDia && !generando);
  }

  $$('.parametros input').forEach((i) => i.addEventListener('input', repasarGenerar));
  $('#con-stl').addEventListener('change', repasarGenerar);

  $('#restaurar').addEventListener('click', () => {
    $$('.parametros input').forEach((i) => {
      i.value = i.dataset.defecto;
    });
    limpiarErroresDeCampo();
    // Asignar `.value` desde el codigo no dispara `input`: hay que repasar.
    repasarGenerar();
  });

  conectarZona($('#zona'), $('#entrada'), (archivos) => {
    archivo = archivos[0];
    texto($('#nombre-archivo'), archivo.name);
    mostrar($('#chip-archivo'), true);
    repasarGenerar();
  });

  if (origen) {
    texto($('#nombre-archivo'), 'viene de la pantalla anterior');
    mostrar($('#chip-archivo'), true);
    repasarGenerar();
  }

  iniciarPaleta();

  generar.addEventListener('click', async () => {
    limpiarErroresDeCampo();
    generando = true;
    repasarGenerar();
    const datos = new FormData();
    if (archivo) datos.append('archivo', archivo, archivo.name);
    else if (origen) datos.append('origen', origen);
    datos.append('modo', modo);
    datos.append('con_stl', $('#con-stl').checked ? 'true' : 'false');
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
   * Primero la vista previa, despues las descargas.
   *
   * El orden es del producto, no una limitacion: lo que se baja es la misma
   * geometria que se esta viendo girar, y verla antes es lo que convierte al
   * boton de descarga en una decision y no en una apuesta. Los enlaces se
   * dibujan enseguida —para que se sepa que salio— pero **sin `href`**, que
   * es la unica forma de que un `<a download>` sea de verdad inerte.
   */
  function pintarResultado(trabajo) {
    mostrar(estado, false);
    texto($('#pista-visor'), 'geometria real del archivo que vas a descargar');

    $$('#botones-descarga a[data-clave]').forEach((a) => {
      const disponible = trabajo.archivos.includes(a.dataset.clave);
      if (disponible) {
        a.dataset.destino = urlArchivo(trabajo.id, a.dataset.clave);
        a.removeAttribute('href');
        a.setAttribute('aria-disabled', 'true');
      }
      mostrar(a, disponible);
    });
    texto($('#pista-descargas'), 'Se habilitan al terminar de cargar la vista previa.');
    mostrar($('#panel-descargas'), true);

    if (trabajo.archivos.includes('glb')) {
      document.dispatchEvent(
        new CustomEvent('cortante:listo', { detail: { url: urlArchivo(trabajo.id, 'glb') } })
      );
      // Red de seguridad: si el modulo del visor ni siquiera pudo cargarse,
      // nadie va a emitir `cortante:preview` y las descargas quedarian
      // esperando para siempre un archivo que ya esta en disco.
      esperaPreview = setTimeout(() => habilitarDescargas(PISTA_SIN_PREVIEW), MS_ESPERA_PREVIEW);
    } else {
      habilitarDescargas('Este trabajo no dejo vista previa, pero los archivos estan completos.');
    }
    pintarReporte(trabajo.reporte);
  }

  function habilitarDescargas(pista) {
    clearTimeout(esperaPreview);
    $$('#botones-descarga a[data-clave]').forEach((a) => {
      if (!a.dataset.destino) return;
      a.href = a.dataset.destino;
      a.removeAttribute('aria-disabled');
    });
    texto($('#pista-descargas'), pista);
  }

  // Si el visor no puede pintar —sin WebGL, o el GLB no carga— las descargas
  // se habilitan igual: el archivo esta bien, lo que fallo es la vista.
  document.addEventListener('cortante:preview', (e) =>
    habilitarDescargas(
      e.detail.ok ? PISTA_CON_PREVIEW : PISTA_SIN_PREVIEW
    )
  );

  /**
   * Paleta del visor.
   *
   * Cambia el color de la pieza en pantalla y nada mas: el `.3mf` y el `.glb`
   * salen con los materiales del motor, y al imprimir el color lo pone el
   * filamento. Por eso no reactiva el boton de generar ni se manda al
   * servidor. Se recuerda en `localStorage`, igual que el tema.
   *
   * La lista de colores la dibuja el template desde `COLORES` del router: aca
   * no hay ningun codigo de color escrito, solo el que trae cada boton.
   */
  function iniciarPaleta() {
    const muestras = $$('#paleta .paleta__color');
    if (!muestras.length) return;

    let guardado = null;
    try {
      guardado = localStorage.getItem('color-visor');
    } catch (_) {
      /* sin localStorage se arranca con el primero, que es el default */
    }

    const aplicar = (boton, recordar) => {
      muestras.forEach((o) => o.setAttribute('aria-pressed', String(o === boton)));
      document.dispatchEvent(
        new CustomEvent('cortante:color', { detail: { color: boton.dataset.color } })
      );
      if (!recordar) return;
      try {
        localStorage.setItem('color-visor', boton.dataset.color);
      } catch (_) {
        /* sin persistencia: el color vale para esta pagina igual */
      }
    };

    muestras.forEach((b) => b.addEventListener('click', () => aplicar(b, true)));
    aplicar(muestras.find((b) => b.dataset.color === guardado) || muestras[0], false);
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
});
