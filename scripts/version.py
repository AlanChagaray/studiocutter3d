"""La version del proyecto: leerla, verificarla, calcular la siguiente y escribirla.

Lo corre el workflow `ci-release.yml`, y existe como script y no como bash
adentro del YAML por una razon: el acarreo al llegar a 99 es una regla con
esquinas, y una regla con esquinas se prueba (`tests/test_version.py`), no se
confia. Un `sed` no se puede testear desde pytest.

**Hay una sola fuente de verdad y tres copias que la espejan.** `app/__init__.py`
es la que lee el proceso web —y la unica que viaja en la imagen, porque el
Dockerfile no instala el paquete y `importlib.metadata` no lo encontraria—;
`pyproject.toml` y `cutter3d/__init__.py` la repiten. `verificar` falla si las
tres no coinciden, y `escribir` las cambia a las tres o a ninguna.

Formato: `MAYOR.MENOR.PARCHE`, sin ceros a la izquierda (PEP 440 los normaliza
igual, asi que `00.02.00` seria otra forma de escribir `0.2.0`, y dos formas de
escribir lo mismo es lo que este script existe para evitar). MENOR y PARCHE van
de 0 a 99; al pasarse vuelven a 0 y suman uno al de la izquierda. MAYOR no tiene
tope.

    python scripts/version.py leer                      → 0.2.0
    python scripts/version.py verificar                 → sale 0 si las tres copias coinciden
    python scripts/version.py siguiente --tipo feat     → 0.3.0
    python scripts/version.py escribir 0.3.0            → reescribe las tres copias
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

TOPE = 99
"""Ultimo valor de MENOR y de PARCHE antes de volver a 0 y acarrear."""

PATRON_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

BUMP_POR_TIPO: dict[str, str] = {
    "break": "mayor",
    "feat": "menor",
    "fix": "parche",
    "hotfix": "parche",
    "perf": "parche",
    "refactor": "parche",
    "docs": "parche",
    "chore": "parche",
    "ci": "parche",
    "test": "parche",
    "style": "parche",
    "build": "parche",
}
"""Que numero sube cada tipo de rama (`feat/...`, `fix/...`).

Solo `feat` sube MENOR y solo `break` sube MAYOR; todo lo demas es PARCHE. La
lista es cerrada a proposito: un tipo que no esta aca hace fallar el release
con el nombre del tipo, en vez de adivinar. Es la misma lista que valida el
nombre de la rama en `ci-quality.yml` — si agregas un tipo, va en los dos.
"""

COPIAS: tuple[tuple[Path, re.Pattern[str]], ...] = (
    (RAIZ / "app" / "__init__.py", re.compile(r'^(__version__ = ")([^"]+)(")$', re.M)),
    (RAIZ / "cutter3d" / "__init__.py", re.compile(r'^(__version__ = ")([^"]+)(")$', re.M)),
    (RAIZ / "pyproject.toml", re.compile(r'^(version = ")([^"]+)(")$', re.M)),
)
"""Donde vive la version. La primera es la fuente; las otras dos, espejos.

Cada patron tiene que matchear EXACTAMENTE una vez en su archivo: en
`pyproject.toml`, `version = "..."` al comienzo de linea solo aparece en
`[project]` (las tools escriben `target-version`, `python_version`, que no
arrancan la linea con `version`)."""


class VersionInvalida(ValueError):
    """La cadena no es `MAYOR.MENOR.PARCHE` o una copia no coincide con la fuente."""


@dataclass(frozen=True, order=True)
class Version:
    mayor: int
    menor: int
    parche: int

    @classmethod
    def parsear(cls, texto: str) -> Version:
        coincidencia = PATRON_VERSION.match(texto.strip())
        if coincidencia is None:
            raise VersionInvalida(
                f"{texto!r} no tiene el formato MAYOR.MENOR.PARCHE (sin ceros a la izquierda)"
            )
        mayor, menor, parche = (int(g) for g in coincidencia.groups())
        if menor > TOPE or parche > TOPE:
            raise VersionInvalida(f"{texto!r}: MENOR y PARCHE van de 0 a {TOPE}")
        return cls(mayor, menor, parche)

    def __str__(self) -> str:
        return f"{self.mayor}.{self.menor}.{self.parche}"

    def siguiente(self, tipo: str) -> Version:
        """La version que sigue segun el tipo de rama, con acarreo al pasar de 99.

        `feat` sube MENOR y pone PARCHE en 0; `break` sube MAYOR y pone los dos en
        0; el resto sube PARCHE. Si el que sube pasa de 99, vuelve a 0 y suma uno
        al de la izquierda — y si ese tambien pasa de 99, sigue subiendo.
        """
        nivel = BUMP_POR_TIPO.get(tipo)
        if nivel is None:
            conocidos = ", ".join(sorted(BUMP_POR_TIPO))
            raise VersionInvalida(f"tipo de rama desconocido: {tipo!r} (conocidos: {conocidos})")
        if nivel == "mayor":
            return Version(self.mayor + 1, 0, 0)
        if nivel == "menor":
            return _con_acarreo(self.mayor, self.menor + 1, 0)
        return _con_acarreo(self.mayor, self.menor, self.parche + 1)


def _con_acarreo(mayor: int, menor: int, parche: int) -> Version:
    if parche > TOPE:
        parche = 0
        menor += 1
    if menor > TOPE:
        menor = 0
        mayor += 1
    return Version(mayor, menor, parche)


def _leer_copia(ruta: Path, patron: re.Pattern[str]) -> str:
    coincidencias = patron.findall(ruta.read_text(encoding="utf-8"))
    if len(coincidencias) != 1:
        raise VersionInvalida(
            f"{ruta.relative_to(RAIZ)}: se esperaba UNA linea de version y hay {len(coincidencias)}"
        )
    valor: str = coincidencias[0][1]
    return valor


def leer(raiz: Path = RAIZ) -> Version:
    """La version de la fuente de verdad (`app/__init__.py`), verificando las copias."""
    valores = {ruta.relative_to(raiz): _leer_copia(ruta, patron) for ruta, patron in _copias(raiz)}
    distintos = set(valores.values())
    if len(distintos) != 1:
        detalle = " · ".join(f"{ruta}={valor}" for ruta, valor in valores.items())
        raise VersionInvalida(f"las copias de la version no coinciden: {detalle}")
    return Version.parsear(distintos.pop())


def escribir(nueva: Version, raiz: Path = RAIZ) -> None:
    """Reescribe las tres copias. Antes verifica que hoy coincidan: no arregla un desorden."""
    leer(raiz)
    for ruta, patron in _copias(raiz):
        texto = ruta.read_text(encoding="utf-8")
        texto, cantidad = patron.subn(rf"\g<1>{nueva}\g<3>", texto)
        if cantidad != 1:  # `leer` ya lo garantizo; la segunda guarda no cuesta nada
            raise VersionInvalida(f"{ruta.relative_to(raiz)}: no se pudo reemplazar la version")
        ruta.write_text(texto, encoding="utf-8", newline="\n")


def _copias(raiz: Path) -> tuple[tuple[Path, re.Pattern[str]], ...]:
    return tuple((raiz / ruta.relative_to(RAIZ), patron) for ruta, patron in COPIAS)


def tipos() -> str:
    """Los tipos de rama aceptados, como alternativa de regex (`break|chore|...`).

    Lo consume el job `rama` de `ci-quality.yml` para validar el nombre de la
    rama del PR: asi la lista vive en un solo lugar y el que agrega un tipo aca
    lo habilita tambien alla.
    """
    return "|".join(sorted(BUMP_POR_TIPO))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--raiz", type=Path, default=RAIZ, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="orden", required=True)
    sub.add_parser("leer", help="imprime la version actual")
    sub.add_parser("verificar", help="sale 0 si las tres copias coinciden y son validas")
    sub.add_parser("tipos", help="imprime los tipos de rama aceptados, separados por |")
    p_sig = sub.add_parser("siguiente", help="imprime la version que sigue")
    p_sig.add_argument("--tipo", required=True, choices=sorted(BUMP_POR_TIPO))
    p_esc = sub.add_parser("escribir", help="reescribe las tres copias con la version dada")
    p_esc.add_argument("version")
    args = parser.parse_args(argv)

    if args.orden == "tipos":  # no necesita leer el repo
        print(tipos())
        return 0

    try:
        actual = leer(args.raiz)
        if args.orden == "leer":
            print(actual)
        elif args.orden == "verificar":
            print(f"version coherente: {actual}")
        elif args.orden == "siguiente":
            print(actual.siguiente(args.tipo))
        else:
            nueva = Version.parsear(args.version)
            escribir(nueva, args.raiz)
            print(f"{actual} -> {nueva}")
    except VersionInvalida as exc:
        print(f"version: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
