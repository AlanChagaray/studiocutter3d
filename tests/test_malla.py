"""F4 — leer una malla de afuera y derivarle el `.glb` que se fotografia.

El criterio de aceptacion de esta pantalla no es "sale una foto": es que la foto
de un archivo viejo sea **la misma** que la de su ciclo. Eso se apoya en dos
afirmaciones medibles, y son las dos que cubren estos tests:

1. El `.glb` derivado describe la misma geometria que el archivo de entrada.
2. El `.glb` derivado lleva el acabado PLA del motor (`metallicFactor` 0,
   `roughnessFactor` 0,78). Sin esto la malla sale sin array `materials` y el
   visor le aplica el default de la spec de glTF —metal rugoso—, porque el front
   solo pisa el color y nunca el acabado.

Las mallas se construyen aca con trimesh en vez de versionar `.3mf` binarios,
por lo mismo que `test_fidelidad.py` no versiona sus salidas: cambiarian con
cada version de manifold3d sin que cambie nada real.
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import numpy as np
import pytest
import trimesh
from shapely.geometry import MultiPolygon, Polygon

import cutter3d.malla as malla_mod
from cutter3d.errors import MallaIlegible
from cutter3d.geometry import Cortador2D, Marcador2D
from cutter3d.malla import (
    MAX_TRIANGULOS,
    ROL_CORTADOR,
    ROL_MARCADOR,
    ROL_UNICO,
    ReporteMalla,
    a_glb,
    cargar_malla,
    convertir,
    cota_de_triangulos,
)
from cutter3d.measure import MedidaTrazo
from cutter3d.paquete3mf import MAX_ENTRADAS_ZIP, confirmar_3mf
from cutter3d.params import CutterParams
from cutter3d.solids import (
    NOMBRE_CORTADOR,
    NOMBRE_MARCADOR,
    aplicar_acabado,
    construir_cortador_3d,
    construir_marcador_3d,
)

RUGOSIDAD_PLA = 0.78
"""El acabado que tiene que sobrevivir a la conversion. Ver `solids._RUGOSIDAD_PLA`."""


# ── Mallas de prueba ─────────────────────────────────────────────────────────


def _escena_de_dos_cuerpos() -> trimesh.Scene:
    """Dos cajas nombradas como los cuerpos que exporta el motor."""
    escena = trimesh.Scene()
    marcador = trimesh.creation.box(extents=(20.0, 10.0, 2.0))
    cortador = trimesh.creation.box(extents=(24.0, 14.0, 8.0))
    cortador.apply_translation((0.0, 0.0, 3.0))
    escena.add_geometry(marcador, geom_name=NOMBRE_MARCADOR)
    escena.add_geometry(cortador, geom_name=NOMBRE_CORTADOR)
    return escena


@pytest.fixture
def tres_mf(tmp_path: Path) -> Path:
    ruta = tmp_path / "entrada.3mf"
    ruta.write_bytes(bytes(_escena_de_dos_cuerpos().export(file_type="3mf")))
    return ruta


@pytest.fixture
def stl(tmp_path: Path) -> Path:
    ruta = tmp_path / "entrada.stl"
    ruta.write_bytes(bytes(trimesh.creation.box(extents=(30.0, 20.0, 9.0)).export(file_type="stl")))
    return ruta


def _materiales(glb: Path) -> list[object]:
    escena = trimesh.load(str(glb), file_type="glb", force="scene")
    assert isinstance(escena, trimesh.Scene)
    return [m.visual.material for m in escena.geometry.values()]


# ── Lo que hace que la foto sea la misma ─────────────────────────────────────


def test_el_glb_derivado_lleva_el_acabado_pla(tres_mf: Path, tmp_path: Path) -> None:
    """La razon de ser del modulo. Si esto se cae, la pieza se ve metalica.

    Un `.3mf` **no transporta materiales**: sin `aplicar_acabado` el glTF sale
    sin array `materials` y el visor usa el default de la spec (metallic 1.0,
    roughness 1.0). El front solo pisa `m.color`, nunca el acabado, asi que
    nadie mas lo corrige aguas abajo.
    """
    a_glb([tres_mf], tmp_path / "salida.glb")
    materiales = _materiales(tmp_path / "salida.glb")
    assert len(materiales) == 2
    for material in materiales:
        assert material.metallicFactor == pytest.approx(0.0)
        assert material.roughnessFactor == pytest.approx(RUGOSIDAD_PLA)


def test_el_stl_sin_nombres_tambien_recibe_acabado(stl: Path, tmp_path: Path) -> None:
    """STL no tiene nombres de objeto: la malla anonima es un cortador."""
    a_glb([stl], tmp_path / "salida.glb")
    (material,) = _materiales(tmp_path / "salida.glb")
    assert material.metallicFactor == pytest.approx(0.0)
    assert material.roughnessFactor == pytest.approx(RUGOSIDAD_PLA)


def test_el_acabado_del_motor_y_el_derivado_son_el_mismo(tmp_path: Path) -> None:
    """F3 y F4 no pueden separarse: `aplicar_acabado` tiene un solo dueño.

    Se compara contra lo que produce `solids` para el cortante de verdad. Si
    alguien cambia el color o la rugosidad en un lado y no en el otro, la foto
    de un archivo viejo deja de coincidir con la de su ciclo — que es justo lo
    que F4 existe para evitar.
    """
    del tmp_path
    propia = aplicar_acabado(trimesh.creation.box(extents=(4.0, 4.0, 4.0)), NOMBRE_CORTADOR)
    assert propia.visual.material.roughnessFactor == pytest.approx(RUGOSIDAD_PLA)
    assert propia.visual.material.metallicFactor == pytest.approx(0.0)
    # Y el marcador tiene su propio color, no el del cortador.
    marcador = aplicar_acabado(trimesh.creation.box(extents=(4.0, 4.0, 4.0)), NOMBRE_MARCADOR)
    assert not np.allclose(
        marcador.visual.material.baseColorFactor, propia.visual.material.baseColorFactor
    )


def test_construir_los_solidos_sigue_poniendo_el_acabado() -> None:
    """`construir_*_3d` pasaron a delegar en `aplicar_acabado`: que no se pierda.

    Se construyen por el camino del motor —no con `aplicar_acabado` a mano— justo
    para que el test note si alguien deshace esa delegacion.
    """
    cuadrado = MultiPolygon([Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])])
    chico = MultiPolygon([Polygon([(4, 4), (16, 4), (16, 16), (4, 16)])])
    p = CutterParams()

    trazo = MedidaTrazo(
        p1=1.0, p5=1.0, mediana=1.0, p95=1.0, px_por_mm_efectivo=1.0, poda_completa=True
    )
    marcador = construir_marcador_3d(
        Marcador2D(
            arte_final=chico,
            arte_original_escalado=chico,
            silueta=cuadrado,
            dilatacion_aplicada_mm=0.0,
            iteraciones=1,
            escala=1.0,
            trazo=trazo,
            mediana_antes_mm=1.0,
        ),
        p,
    )
    cortador = construir_cortador_3d(
        Cortador2D(filo=cuadrado, pie=cuadrado, o1=chico, o2=cuadrado, o3=cuadrado), p
    )

    for malla in (marcador, cortador):
        assert malla.visual.material.metallicFactor == pytest.approx(0.0)
        assert malla.visual.material.roughnessFactor == pytest.approx(RUGOSIDAD_PLA)
    # Y cada cuerpo conserva SU color: pintarlos igual seria perder la distincion
    # que el motor pone en el `.glb` y que el front recien despues unifica.
    assert not np.allclose(
        marcador.visual.material.baseColorFactor, cortador.visual.material.baseColorFactor
    )


# ── Que el glb sea el mismo archivo ──────────────────────────────────────────


def test_la_conversion_preserva_geometria_y_medidas(tres_mf: Path, tmp_path: Path) -> None:
    reporte = a_glb([tres_mf], tmp_path / "salida.glb")
    entrada = trimesh.load(str(tres_mf), file_type="3mf", force="scene")

    assert isinstance(reporte, ReporteMalla)
    assert reporte.triangulos == sum(len(m.faces) for m in entrada.geometry.values())
    assert reporte.medidas_mm == pytest.approx(tuple(round(float(v), 3) for v in entrada.extents))
    assert reporte.cerrado is True
    assert reporte.advertencias == ()


def test_los_cuerpos_nombrados_del_motor_se_conservan(tres_mf: Path, tmp_path: Path) -> None:
    reporte = a_glb([tres_mf], tmp_path / "salida.glb")
    assert reporte.objetos == (NOMBRE_MARCADOR, NOMBRE_CORTADOR)


def test_un_stl_no_filtra_el_nombre_del_archivo_en_disco(stl: Path, tmp_path: Path) -> None:
    """STL no tiene nombres de objeto y trimesh usa el del archivo.

    Ese nombre es siempre `entrada.stl` —lo pone la capa web— asi que mostrarlo
    seria filtrar un detalle interno sin informar nada.
    """
    reporte = a_glb([stl], tmp_path / "salida.glb")
    assert reporte.objetos == ("cuerpo",)
    assert not any("entrada" in o for o in reporte.objetos)


def test_una_malla_abierta_advierte_pero_no_falla(tmp_path: Path) -> None:
    """Un archivo viejo imperfecto igual merece su foto: esta pantalla no imprime.

    Es la aplicacion de la politica del motor: falla duro lo que produciria un
    archivo invalido, **advierte** lo que produce uno valido pero dificil de
    imprimir.
    """
    caja = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    caja.update_faces(np.arange(len(caja.faces)) != 0)  # se le saca una cara
    ruta = tmp_path / "entrada.stl"
    ruta.write_bytes(bytes(caja.export(file_type="stl")))

    reporte = a_glb([ruta], tmp_path / "salida.glb")
    assert reporte.cerrado is False
    assert reporte.advertencias, "una malla abierta tiene que decirse"
    assert (tmp_path / "salida.glb").is_file(), "la foto se puede sacar igual"


# ── Lo que se rechaza ────────────────────────────────────────────────────────


def test_un_zip_que_no_es_3mf_se_rechaza(tmp_path: Path) -> None:
    """La firma `PK\\x03\\x04` la comparten docx, xlsx, jar y epub."""
    ruta = tmp_path / "entrada.3mf"
    with zipfile.ZipFile(ruta, "w") as z:
        z.writestr("word/document.xml", "<w:document/>")
    with pytest.raises(MallaIlegible) as exc:
        confirmar_3mf(ruta)
    assert "3MF" in exc.value.motivo


def test_un_zip_con_demasiadas_entradas_se_rechaza(tmp_path: Path) -> None:
    ruta = tmp_path / "entrada.3mf"
    with zipfile.ZipFile(ruta, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
        for i in range(MAX_ENTRADAS_ZIP + 1):
            z.writestr(f"relleno/{i}.txt", "x")
    with pytest.raises(MallaIlegible):
        confirmar_3mf(ruta)


def test_un_archivo_que_no_es_zip_se_rechaza(tmp_path: Path) -> None:
    ruta = tmp_path / "entrada.3mf"
    ruta.write_bytes(b"esto no es un zip ni de casualidad")
    with pytest.raises(MallaIlegible):
        confirmar_3mf(ruta)


def test_una_extension_no_soportada_se_rechaza(tmp_path: Path) -> None:
    ruta = tmp_path / "entrada.obj"
    ruta.write_text("v 0 0 0\n", encoding="utf-8")
    with pytest.raises(MallaIlegible) as exc:
        cargar_malla(ruta)
    assert ".obj" in exc.value.motivo


def test_un_stl_vacio_se_rechaza(tmp_path: Path) -> None:
    """Cabecera de 80 bytes + contador en cero: es un STL valido y sin triangulos."""
    ruta = tmp_path / "entrada.stl"
    ruta.write_bytes(b"\x00" * 80 + struct.pack("<I", 0))
    with pytest.raises(MallaIlegible):
        cargar_malla(ruta)


def test_la_cota_de_triangulos_no_necesita_abrir_la_malla(stl: Path) -> None:
    """La defensa que funciona en Windows, donde el hijo no tiene RLIMIT_DATA."""
    reales = len(trimesh.load(str(stl), file_type="stl").faces)
    assert cota_de_triangulos(stl, "stl") == reales
    # Para 3MF no hay forma barata: ahi el techo lo pone `MAX_DESCOMPRIMIDO`.
    assert cota_de_triangulos(stl, "3mf") is None


def test_un_stl_declarado_gigante_se_rechaza_sin_materializarlo(tmp_path: Path) -> None:
    """La cabecera miente el contador y el archivo es minusculo: se rechaza igual.

    Importa que sea `cota_de_triangulos` y no el conteo posterior: contar
    despues de cargar llega tarde justo en el caso que la cota existe para
    cubrir.
    """
    ruta = tmp_path / "entrada.stl"
    declarados = MAX_TRIANGULOS * 2
    ruta.write_bytes(b"\x00" * 80 + struct.pack("<I", declarados))
    assert cota_de_triangulos(ruta, "stl") <= MAX_TRIANGULOS  # el tamaño real manda

    # Con el archivo realmente grande, la cota si dispara.
    grande = tmp_path / "grande.stl"
    grande.write_bytes(b"\x00" * (MAX_TRIANGULOS * 50 + 200))
    assert cota_de_triangulos(grande, "stl") > MAX_TRIANGULOS
    with pytest.raises(MallaIlegible) as exc:
        cargar_malla(grande)
    assert "triangulos" in exc.value.motivo


# ── Un diseño en dos archivos ────────────────────────────────────────────────


def test_un_diseno_partido_en_dos_da_lo_mismo_que_el_combinado(tmp_path: Path) -> None:
    """La afirmacion central de este ciclo, y es medible.

    El motor exporta el cortante y su marcador juntos en un `.3mf` y tambien
    sueltos en dos archivos. Las dos formas describen **la misma pieza**, asi que
    la foto tiene que ser la misma: si las coordenadas no se conservaran al unir,
    el marcador saldria corrido respecto del cortador y nadie lo notaria hasta
    mirar la foto.
    """
    escena = _escena_de_dos_cuerpos()
    combinado = tmp_path / "entrada.3mf"
    combinado.write_bytes(bytes(escena.export(file_type="3mf")))
    cortador = tmp_path / "entrada-01a.stl"
    marcador = tmp_path / "entrada-01b.stl"
    cortador.write_bytes(bytes(escena.geometry[NOMBRE_CORTADOR].export(file_type="stl")))
    marcador.write_bytes(bytes(escena.geometry[NOMBRE_MARCADOR].export(file_type="stl")))

    uno = a_glb([combinado], tmp_path / "uno.glb", [ROL_UNICO])
    dos = a_glb([cortador, marcador], tmp_path / "dos.glb", [ROL_CORTADOR, ROL_MARCADOR])

    assert dos.triangulos == uno.triangulos
    assert dos.medidas_mm == pytest.approx(uno.medidas_mm)
    assert dos.volumen_mm3 == pytest.approx(uno.volumen_mm3)
    assert sorted(dos.objetos) == sorted(uno.objetos)


def test_el_rol_decide_el_color_de_cada_cuerpo(tmp_path: Path) -> None:
    """Sin rol no se puede saber cual es cual: un STL no tiene nombres de objeto.

    El color lo pisa despues la paleta de la pantalla, pero el `.glb` tambien se
    puede bajar: que el marcador salga con su color es lo que hace que ese
    archivo describa la pieza y no una aproximacion.
    """
    escena = _escena_de_dos_cuerpos()
    cortador = tmp_path / "a.stl"
    marcador = tmp_path / "b.stl"
    cortador.write_bytes(bytes(escena.geometry[NOMBRE_CORTADOR].export(file_type="stl")))
    marcador.write_bytes(bytes(escena.geometry[NOMBRE_MARCADOR].export(file_type="stl")))

    a_glb([cortador, marcador], tmp_path / "s.glb", [ROL_CORTADOR, ROL_MARCADOR])
    rt = trimesh.load(str(tmp_path / "s.glb"), file_type="glb", force="scene")
    colores = {n: tuple(m.visual.material.baseColorFactor) for n, m in rt.geometry.items()}
    assert set(colores) == {NOMBRE_CORTADOR, NOMBRE_MARCADOR}
    assert colores[NOMBRE_CORTADOR] != colores[NOMBRE_MARCADOR]


def test_dos_archivos_con_cuerpos_homonimos_no_se_pisan(tmp_path: Path) -> None:
    """`add_geometry` con un nombre repetido REEMPLAZA en silencio.

    Dos `.3mf` combinados agrupados a mano traen los dos un `cortador` y un
    `marcador`; sin desambiguar, el segundo archivo se comeria al primero y el
    diseño saldria a medias sin un solo error.
    """
    uno = tmp_path / "a.3mf"
    otro = tmp_path / "b.3mf"
    uno.write_bytes(bytes(_escena_de_dos_cuerpos().export(file_type="3mf")))
    otro.write_bytes(bytes(_escena_de_dos_cuerpos().export(file_type="3mf")))

    reporte = a_glb([uno, otro], tmp_path / "s.glb", [ROL_UNICO, ROL_UNICO])
    rt = trimesh.load(str(tmp_path / "s.glb"), file_type="glb", force="scene")
    assert len(rt.geometry) == 4, "los cuatro cuerpos tienen que sobrevivir"
    assert reporte.triangulos == 2 * sum(
        len(m.faces) for m in _escena_de_dos_cuerpos().geometry.values()
    )


def test_el_techo_de_triangulos_vale_para_el_diseno_entero(tmp_path: Path) -> None:
    """Dos mitades que pasan sueltas pueden no entrar juntas."""
    escena = _escena_de_dos_cuerpos()
    a = tmp_path / "a.stl"
    b = tmp_path / "b.stl"
    a.write_bytes(bytes(escena.geometry[NOMBRE_CORTADOR].export(file_type="stl")))
    b.write_bytes(bytes(escena.geometry[NOMBRE_MARCADOR].export(file_type="stl")))

    caras = len(trimesh.load(str(a), file_type="stl").faces)
    original = malla_mod.MAX_TRIANGULOS
    # Un techo que deja pasar cada archivo suelto pero no la suma.
    malla_mod.MAX_TRIANGULOS = caras + 1
    try:
        a_glb([a], tmp_path / "a.glb", [ROL_UNICO])  # suelto entra
        with pytest.raises(MallaIlegible, match="el diseño suma"):
            a_glb([a, b], tmp_path / "s.glb", [ROL_CORTADOR, ROL_MARCADOR])
    finally:
        malla_mod.MAX_TRIANGULOS = original


def test_un_diseno_sin_archivos_o_con_demasiados_se_rechaza(tmp_path: Path, stl: Path) -> None:
    with pytest.raises(MallaIlegible):
        a_glb([], tmp_path / "s.glb")
    with pytest.raises(MallaIlegible, match="hasta 2 archivos"):
        a_glb([stl, stl, stl], tmp_path / "s.glb")


# ── Conversion entre formatos de malla ───────────────────────────────────────
#
# Lo que hay que sostener aca es una sola afirmacion: **el archivo que sale
# describe la misma pieza que el que entro**. No alcanza con que abra — un
# conversor que repara, centra o reorienta tambien abre, y entrega otra cosa.
# Por eso los asserts son numericos y comparan contra la ENTRADA, no contra
# constantes escritas a mano.


def _con_unidad(origen: Path, destino: Path, unidad: str) -> Path:
    """Copia un `.3mf` cambiandole la unidad declarada en el XML.

    Se reescribe el ZIP en vez de construir el 3MF a mano porque lo que se
    quiere probar es la lectura de un archivo de otro programa, y trimesh
    siempre escribe `millimeter`.
    """
    with zipfile.ZipFile(origen) as entrada, zipfile.ZipFile(destino, "w") as salida:
        for info in entrada.infolist():
            datos = entrada.read(info.filename)
            if info.filename.endswith(".model"):
                datos = datos.replace(b'unit="millimeter"', f'unit="{unidad}"'.encode())
            salida.writestr(info, datos)
    return destino


def test_un_3mf_a_stl_conserva_triangulos_medidas_y_volumen(tres_mf: Path, tmp_path: Path) -> None:
    """El caso normal, medido contra la entrada y no contra numeros a mano."""
    entrada = cargar_malla(tres_mf)
    reporte = convertir(tres_mf, tmp_path / "salida.stl")

    assert reporte.origen == "3mf"
    assert reporte.destino == "stl"
    assert reporte.triangulos == sum(len(m.faces) for m in entrada.geometry.values())
    assert np.allclose(reporte.medidas_mm, entrada.extents, atol=1e-3)
    assert reporte.volumen_mm3 == pytest.approx(
        sum(abs(m.volume) for m in entrada.geometry.values()), rel=1e-6
    )
    assert reporte.cerrado is True


def test_un_stl_a_3mf_no_mueve_un_solo_vertice(stl: Path, tmp_path: Path) -> None:
    """3MF guarda las coordenadas como texto, asi que el roundtrip es EXACTO.

    Se compara vertice a vertice y no por la caja: una pieza espejada o rotada
    90 grados tiene la misma caja y no es la misma pieza.
    """
    antes = trimesh.load(str(stl), file_type="stl")
    convertir(stl, tmp_path / "salida.3mf")
    despues = trimesh.load(str(tmp_path / "salida.3mf"), file_type="3mf", force="scene").to_mesh()

    assert len(despues.faces) == len(antes.faces)
    assert np.array_equal(np.sort(despues.vertices, axis=0), np.sort(antes.vertices, axis=0))


def test_ir_a_stl_no_mueve_las_piezas_mas_que_lo_que_float32_permite(
    tres_mf: Path, tmp_path: Path
) -> None:
    """La unica perdida admitida es la del formato: STL guarda en `float32`.

    Medido en este repo, un cortante real hace el roundtrip **bit a bit**; el
    techo de 1e-4 mm sobre una pieza de 24 mm deja tres ordenes de magnitud de
    aire y se cae al toque si algo empieza a redondear de verdad.
    """
    antes = cargar_malla(tres_mf).to_mesh()
    convertir(tres_mf, tmp_path / "salida.stl")
    despues = trimesh.load(str(tmp_path / "salida.stl"), file_type="stl")

    ordenados = (np.sort(despues.vertices, axis=0), np.sort(antes.vertices, axis=0))
    assert np.allclose(*ordenados, atol=1e-4)


def test_la_union_de_cuerpos_al_ir_a_stl_se_declara(tres_mf: Path, tmp_path: Path) -> None:
    """STL no sabe contener dos objetos, y eso el usuario tiene que saberlo.

    No es una perdida de geometria —las coordenadas quedan donde estaban, y eso
    lo cubre el test de los vertices— pero si de estructura: el slicer ya no
    puede mover el marcador sin el cortador. Como todo lo que este proyecto
    toca, se declara con el numero.
    """
    reporte = convertir(tres_mf, tmp_path / "salida.stl")
    assert reporte.cuerpos_unidos == 2
    assert any("2 objetos" in a for a in reporte.advertencias)


def test_del_stl_al_3mf_no_se_une_nada(stl: Path, tmp_path: Path) -> None:
    """La otra direccion no tiene nada que unir: un STL ya es un solo cuerpo."""
    reporte = convertir(stl, tmp_path / "salida.3mf")
    assert reporte.cuerpos_unidos == 0
    assert reporte.advertencias == ()


def test_un_3mf_en_pulgadas_sale_en_milimetros_y_lo_declara(tres_mf: Path, tmp_path: Path) -> None:
    """⚠ Sin esto, "no cambiar las medidas" se rompe justo donde importa.

    Un 3MF declara su unidad; un STL no tiene ninguna y todo el ecosistema de
    impresion lo lee en mm. Pasar las coordenadas tal cual entregaria la misma
    pieza **25,4 veces mas chica**, sin un solo error a la vista — y el usuario
    lo descubriria recien con la galletita en la mano.
    """
    pulgadas = _con_unidad(tres_mf, tmp_path / "pulgadas.3mf", "inch")
    reporte = convertir(pulgadas, tmp_path / "salida.stl")

    assert reporte.unidad_origen == "inch"
    assert reporte.escala_a_mm == 25.4
    assert np.allclose(reporte.medidas_mm, (24.0 * 25.4, 14.0 * 25.4, 8.0 * 25.4), atol=1e-2)
    assert any("milimetros" in a for a in reporte.advertencias)


def test_un_3mf_en_milimetros_no_se_escala(tres_mf: Path, tmp_path: Path) -> None:
    """El caso normal no paga nada y no ensucia el reporte con una advertencia."""
    reporte = convertir(tres_mf, tmp_path / "salida.stl")
    assert reporte.escala_a_mm == 1.0
    assert not any("milimetros" in a for a in reporte.advertencias)


def test_una_unidad_desconocida_no_se_asume_en_milimetros(tres_mf: Path, tmp_path: Path) -> None:
    """Nada de fallbacks silenciosos: una escala que no se entiende, falla.

    Asumir mm seria adivinar, y adivinar mal entrega una pieza del tamaño
    equivocado sin decirlo. `unit_conversion` de trimesh ademas acepta formas
    como `"1.21 * meters"`, que el 3MF no permite: por eso la lista es cerrada.
    """
    raro = _con_unidad(tres_mf, tmp_path / "raro.3mf", "1.21 * meters")
    with pytest.raises(MallaIlegible):
        convertir(raro, tmp_path / "salida.stl")


def test_el_3mf_derivado_de_un_stl_no_filtra_el_nombre_en_disco(stl: Path, tmp_path: Path) -> None:
    """STL no tiene nombres de objeto: trimesh usa el del archivo, que es interno.

    La capa web guarda todo como `entrada.<ext>`, asi que sin renombrar el 3MF
    saldria con un `<object name="entrada">` adentro — un detalle del servidor
    metido en un archivo que el usuario se lleva, y que ademas no informa nada.
    """
    reporte = convertir(stl, tmp_path / "salida.3mf")
    assert reporte.objetos == ("cuerpo",)

    with zipfile.ZipFile(tmp_path / "salida.3mf") as z:
        modelo = next(n for n in z.namelist() if n.endswith(".model"))
        xml = z.read(modelo).decode("utf-8")
    assert stl.stem not in xml
    assert 'name="cuerpo"' in xml


def test_una_malla_abierta_se_convierte_igual_y_se_declara_abierta(tmp_path: Path) -> None:
    """No se repara nada. La regla del proyecto es medir y reportar, no compensar.

    Cerrarle el agujero seria cambiar el diseño —justo lo que el pedido prohibe—
    y hacerlo en silencio, encima. Se convierte, y se avisa.
    """
    caja = trimesh.creation.box(extents=(10.0, 10.0, 4.0))
    abierta = trimesh.Trimesh(vertices=caja.vertices, faces=caja.faces[:-1])
    entrada = tmp_path / "abierta.stl"
    entrada.write_bytes(bytes(abierta.export(file_type="stl")))

    reporte = convertir(entrada, tmp_path / "salida.3mf")
    assert reporte.cerrado is False
    assert reporte.triangulos == len(abierta.faces)
    assert any("cerrado" in a for a in reporte.advertencias)


def test_un_destino_que_no_es_malla_no_se_intenta(stl: Path, tmp_path: Path) -> None:
    """El tipo sale de la extension del destino, que la pone `NOMBRE_DE`.

    Nunca lo elige el cliente, pero el modulo no depende de eso: una extension
    fuera de `SUFIJOS` falla antes de escribir nada.
    """
    with pytest.raises(MallaIlegible):
        convertir(stl, tmp_path / "salida.obj")
