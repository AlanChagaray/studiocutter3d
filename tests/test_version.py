"""El acarreo de la version y la coherencia de sus tres copias.

Se importa el script por ruta y no como paquete: `scripts/` no se instala ni
viaja en la imagen, y no tiene por que. Lo que se prueba es la regla —que
`feat` suba MENOR, que 99 vuelva a 0 y acarree— y el contrato con el disco: que
`escribir` toque las tres copias o ninguna.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app import __version__

RAIZ = Path(__file__).resolve().parent.parent


def _cargar() -> ModuleType:
    ruta = RAIZ / "scripts" / "version.py"
    spec = importlib.util.spec_from_file_location("version_script", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    # Antes de ejecutarlo, no despues: `dataclasses` resuelve las anotaciones
    # (que con `from __future__ import annotations` son strings) mirando
    # `sys.modules[cls.__module__]`, y sin esta linea ese lookup da None.
    sys.modules[spec.name] = modulo
    spec.loader.exec_module(modulo)
    return modulo


version = _cargar()


@pytest.mark.parametrize(
    ("actual", "tipo", "esperada"),
    [
        ("0.2.0", "fix", "0.2.1"),
        ("0.2.0", "feat", "0.3.0"),
        ("0.2.7", "feat", "0.3.0"),  # un feat pone el parche en cero
        ("0.2.0", "break", "1.0.0"),
        ("3.45.12", "break", "4.0.0"),
        ("0.2.99", "fix", "0.3.0"),  # 99 + 1 acarrea al menor
        ("0.99.99", "fix", "1.0.0"),  # y si el menor tambien esta en 99, al mayor
        ("0.99.3", "feat", "1.0.0"),
        ("12.0.0", "hotfix", "12.0.1"),
        ("12.0.0", "docs", "12.0.1"),
    ],
)
def test_la_version_siguiente_sube_el_numero_que_dice_el_tipo(
    actual: str, tipo: str, esperada: str
) -> None:
    assert str(version.Version.parsear(actual).siguiente(tipo)) == esperada


def test_un_tipo_de_rama_desconocido_falla_con_su_nombre() -> None:
    with pytest.raises(version.VersionInvalida, match="release"):
        version.Version.parsear("0.2.0").siguiente("release")


@pytest.mark.parametrize("texto", ["1.2", "v1.2.3", "01.2.3", "1.100.0", "1.2.100", "1.2.3.4", ""])
def test_una_version_mal_escrita_no_se_acepta(texto: str) -> None:
    with pytest.raises(version.VersionInvalida):
        version.Version.parsear(texto)


def test_las_tres_copias_del_repo_coinciden_hoy() -> None:
    """Si esto falla, alguien edito una copia a mano. La CI lo frena en el PR."""
    assert str(version.leer()) == __version__


def test_los_tipos_de_rama_son_los_del_bump() -> None:
    """`ci-quality.yml` valida el nombre de la rama con esta misma lista."""
    assert version.tipos() == "|".join(sorted(version.BUMP_POR_TIPO))
    assert "feat" in version.tipos() and "hotfix" in version.tipos()


def _repo_de_juguete(destino: Path, valor: str, *, pyproject: str | None = None) -> Path:
    (destino / "app").mkdir(parents=True)
    (destino / "cutter3d").mkdir()
    (destino / "app" / "__init__.py").write_text(f'"""doc"""\n\n__version__ = "{valor}"\n')
    (destino / "cutter3d" / "__init__.py").write_text(
        f'"""doc"""\n\nimport importlib\n\n__version__ = "{valor}"\n'
    )
    (destino / "pyproject.toml").write_text(
        pyproject
        or (
            "[project]\n"
            'name = "x"\n'
            f'version = "{valor}"\n'
            "\n[tool.ruff]\n"
            'target-version = "py313"\n'
            "\n[tool.mypy]\n"
            'python_version = "3.13"\n'
        )
    )
    return destino


def test_escribir_cambia_las_tres_copias_y_nada_mas(tmp_path: Path) -> None:
    raiz = _repo_de_juguete(tmp_path, "0.2.0")
    antes = {r: r.read_text() for r in raiz.rglob("*") if r.is_file()}

    version.escribir(version.Version.parsear("0.3.0"), raiz)

    assert str(version.leer(raiz)) == "0.3.0"
    for ruta, texto_antes in antes.items():
        texto_despues = ruta.read_text()
        # Un solo cambio por archivo: la linea de la version. Lo demas, intacto.
        assert texto_despues.replace("0.3.0", "0.2.0") == texto_antes
    # `target-version` y `python_version` de las tools no se tocaron.
    assert 'target-version = "py313"' in (raiz / "pyproject.toml").read_text()
    assert 'python_version = "3.13"' in (raiz / "pyproject.toml").read_text()


def test_si_una_copia_difiere_no_se_escribe_ninguna(tmp_path: Path) -> None:
    raiz = _repo_de_juguete(tmp_path, "0.2.0")
    (raiz / "cutter3d" / "__init__.py").write_text('__version__ = "0.1.0"\n')
    antes = {r: r.read_text() for r in raiz.rglob("*") if r.is_file()}

    with pytest.raises(version.VersionInvalida, match="no coinciden"):
        version.escribir(version.Version.parsear("0.3.0"), raiz)

    assert {r: r.read_text() for r in raiz.rglob("*") if r.is_file()} == antes


def test_la_cli_imprime_la_siguiente_y_falla_con_codigo_uno_si_hay_desorden(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raiz = _repo_de_juguete(tmp_path, "0.2.99")
    assert version.main(["--raiz", str(raiz), "siguiente", "--tipo", "fix"]) == 0
    assert capsys.readouterr().out.strip() == "0.3.0"

    shutil.copy(RAIZ / "app" / "__init__.py", raiz / "app" / "__init__.py")  # otra version
    assert version.main(["--raiz", str(raiz), "verificar"]) == 1
    assert "no coinciden" in capsys.readouterr().err
