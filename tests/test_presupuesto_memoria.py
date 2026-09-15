"""El presupuesto de memoria: reducir lo grande, declararlo, y no morir.

El ciclo 6 arranco de un error de Render —`Ran out of memory (used over 512MB)`—
y la causa no era ningun caso patologico: una foto de telefono de 12 MP hacia
picar F2 en **830 MB** medidos, porque
`medial_axis(..., return_distance=True)` cuesta ~63 MB por megapixel y nada
acotaba el tamaño con el que se trabajaba.

Estos tests fijan las tres piezas de la defensa:

1. el presupuesto reduce lo que se pasa (`MAX_PIXELES_TRABAJO`),
2. la reduccion se **declara** en vez de hacerse en silencio,
3. cuando aun asi no alcanza, el que falla es el trabajo y no el servidor.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.errores import traducir
from app.tareas import LIMITE_RAM_HIJO_MB, _acotar_memoria
from cutter3d.raster import (
    MAX_PIXELES,
    MAX_PIXELES_TRABAJO,
    convertir_a_jpg,
    preparar_lineas,
    tamano_de,
    tamano_de_trabajo,
)


def _lineart(ancho: int, alto: int, destino: Path) -> Path:
    """Un JPG con trazo real: F2 necesita tinta para medir el ancho."""
    lienzo = np.full((alto, ancho), 255, dtype=np.uint8)
    grosor = max(2, ancho // 200)
    for i in range(1, 6):
        y = alto * i // 6
        lienzo[y : y + grosor, :] = 0
        x = ancho * i // 6
        lienzo[:, x : x + grosor] = 0
    Image.fromarray(lienzo, mode="L").convert("RGB").save(destino, format="JPEG", quality=92)
    return destino


# ── El presupuesto y su cuenta ──────────────────────────────────────────────


def test_el_presupuesto_de_trabajo_es_mucho_menor_que_el_de_aceptacion() -> None:
    """Son dos limites con dos roles, y confundirlos es lo que tiraba el server.

    `MAX_PIXELES` dice que se acepta decodificar (cubre la camara de 61 MP);
    `MAX_PIXELES_TRABAJO` dice a que tamaño se procesa. Si alguien los iguala
    "para simplificar", vuelve el OOM.
    """
    assert MAX_PIXELES_TRABAJO < MAX_PIXELES


def test_tamano_de_trabajo_no_toca_lo_que_ya_entra() -> None:
    assert tamano_de_trabajo(800, 600) == (800, 600)


def test_tamano_de_trabajo_respeta_el_aspecto_y_el_presupuesto() -> None:
    ancho, alto = tamano_de_trabajo(12_000, 9_000)
    assert ancho * alto <= MAX_PIXELES_TRABAJO
    assert ancho / alto == pytest.approx(12_000 / 9_000, rel=0.01)


def test_tamano_de_trabajo_nunca_devuelve_cero() -> None:
    """Una tira larguisima y de un pixel de alto no puede reducirse a nada."""
    ancho, alto = tamano_de_trabajo(60_000, 1)
    assert ancho >= 1
    assert alto >= 1


# ── F2: se reduce, y se declara ─────────────────────────────────────────────


def test_f2_declara_que_no_redujo_cuando_la_imagen_entra(tmp_path: Path) -> None:
    jpg = _lineart(400, 300, tmp_path / "chica.jpg")
    resultado = preparar_lineas(jpg)
    assert resultado.tamano_original == (400, 300)
    assert resultado.tamano_usado == (400, 300)
    assert not resultado.fue_reducida


def test_f2_reduce_lo_que_se_pasa_del_presupuesto(tmp_path: Path) -> None:
    lado = int((MAX_PIXELES_TRABAJO * 4) ** 0.5)  # 4x el presupuesto
    jpg = _lineart(lado, lado, tmp_path / "grande.jpg")

    resultado = preparar_lineas(jpg)

    assert resultado.fue_reducida
    assert resultado.tamano_original == (lado, lado)
    ancho, alto = resultado.tamano_usado
    assert ancho * alto <= MAX_PIXELES_TRABAJO
    # El PNG de salida es el de la escala reducida, no el original: por eso
    # `tamano_usado` se declara, y por eso `ancho_trazo_px` va en esa escala.
    assert resultado.imagen.shape == (alto, ancho)


def test_f2_sigue_siendo_binario_puro_despues_de_reducir(tmp_path: Path) -> None:
    """La reduccion es con Lanczos, que mete grises. El umbral va DESPUES.

    Si alguien moviera el reescalado al final del pipeline, la salida dejaria de
    tener dos valores puros y F3 recibiria un SVG trazado sobre bordes sucios.
    """
    lado = int((MAX_PIXELES_TRABAJO * 4) ** 0.5)
    jpg = _lineart(lado, lado, tmp_path / "grande.jpg")
    resultado = preparar_lineas(jpg)
    assert set(np.unique(resultado.imagen).tolist()) <= {0, 255}


# ── F1: el conversor tambien esta acotado ───────────────────────────────────


def test_f1_reduce_al_presupuesto(tmp_path: Path) -> None:
    """El conversor corre en el proceso web, sin timeout ni aislamiento.

    Es el unico camino pesado fuera del sandbox del proceso hijo: si no
    estuviera acotado, una foto grande picaria dentro del server mismo.
    """
    lado = int((MAX_PIXELES_TRABAJO * 4) ** 0.5)
    origen = _lineart(lado, lado, tmp_path / "origen.jpg")

    salida = convertir_a_jpg(origen, tmp_path / "salida.jpg")

    tamano = tamano_de(salida)
    assert tamano is not None
    assert tamano[0] * tamano[1] <= MAX_PIXELES_TRABAJO


def test_tamano_de_devuelve_none_ante_algo_que_no_es_imagen(tmp_path: Path) -> None:
    basura = tmp_path / "no-es-imagen.jpg"
    basura.write_bytes(b"esto no es un jpg")
    assert tamano_de(basura) is None


# ── El techo del proceso hijo ───────────────────────────────────────────────


def test_el_techo_de_ram_deja_lugar_al_pico_medido_del_cortante() -> None:
    """277 MB es el `VmData` medido EN EL CONTENEDOR con tres cortantes seguidos.

    El techo tiene que estar por encima: frena lo patologico, no lo pesado. Si
    alguien lo baja de ahi, los cortantes normales empiezan a fallar.

    ⚠ El numero es de `VmData` (heap anonimo), que es lo que acota `RLIMIT_DATA`.
    No confundirlo con `VmPeak`, el espacio de direcciones, que para el mismo
    trabajo da **611 MB**: dimensionar el techo contra la metrica equivocada hace
    fallar hasta la estrella, y ya paso una vez.
    """
    assert LIMITE_RAM_HIJO_MB > 277


@pytest.mark.skipif(sys.platform == "win32", reason="`resource` es POSIX")
def test_el_techo_acota_el_heap_y_no_el_espacio_de_direcciones() -> None:
    """`RLIMIT_AS` seria el limite equivocado: ver el docstring de arriba."""
    import resource  # noqa: PLC0415 — POSIX only, por eso el skipif

    _acotar_memoria()
    assert resource.getrlimit(resource.RLIMIT_AS)[0] == resource.RLIM_INFINITY


def test_acotar_memoria_no_explota_en_ninguna_plataforma() -> None:
    """En Windows es un no-op; en POSIX pone el limite. Nunca levanta."""
    _acotar_memoria()


@pytest.mark.skipif(sys.platform == "win32", reason="`resource` es POSIX")
def test_acotar_memoria_baja_el_limite_de_verdad() -> None:
    import resource  # noqa: PLC0415 — POSIX only, por eso el skipif de arriba

    _acotar_memoria()
    blando, _ = resource.getrlimit(resource.RLIMIT_DATA)
    assert blando <= LIMITE_RAM_HIJO_MB * 1024 * 1024


def test_sin_memoria_no_sale_como_error_interno() -> None:
    """Un `MemoryError` es el techo funcionando, no un fallo del servidor.

    Tiene que llegar al usuario con un codigo propio y un mensaje que diga que
    hacer, no con el generico que lo manda a mirar el log de un servidor al que
    no tiene acceso.
    """
    api = traducir(MemoryError())
    assert api.codigo == "sin_memoria"
    assert api.estado == 413
    assert "memoria" in api.mensaje.lower()
