"""Credenciales y secreto de firma de la sesion.

Dos responsabilidades, las dos criticas:

1. **Verificar credenciales sin filtrar cuales existen.** Si el usuario no
   existe se verifica igual contra un hash señuelo, para que el tiempo de
   respuesta y el resultado sean indistinguibles de una contraseña equivocada.
   Sin eso, medir la latencia del login enumera usuarios.
2. **Obtener el secreto de la sesion.** Sale de la variable de entorno si esta;
   si no, de `sesion.key`; y si tampoco, se genera y se persiste. Nunca esta
   hardcodeado y nunca vive en un `.env`.

La contraseña en claro no se escribe, no se loguea y no sale de esta funcion.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

from .config import Ajustes, ErrorDeConfiguracion

LARGO_MINIMO_SECRETO = 32
_hasher = PasswordHasher()

_HASH_SENUELO = _hasher.hash(secrets.token_urlsafe(32))
"""Hash contra el que se verifica cuando el usuario no existe.

Se calcula una vez al importar el modulo, sobre un valor aleatorio que nadie
conoce: verificar contra el siempre falla, pero **cuesta lo mismo** que
verificar contra un hash real. Es lo que hace que el login no revele si un
usuario existe.
"""


def cargar_usuarios(a: Ajustes) -> dict[str, str]:
    """Lee `credenciales.json` y devuelve {usuario: hash}."""
    if not a.archivo_credenciales.is_file():
        raise ErrorDeConfiguracion(
            f"falta {a.archivo_credenciales.name}: no hay ningun usuario dado de alta"
        )
    try:
        datos = json.loads(a.archivo_credenciales.read_text(encoding="utf-8"))
        entradas = datos["usuarios"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ErrorDeConfiguracion(
            f"{a.archivo_credenciales.name} no tiene el formato esperado "
            '({"usuarios": [{"usuario": ..., "hash": ...}]})'
        ) from exc
    return {str(e["usuario"]): str(e["hash"]) for e in entradas}


def verificar_credenciales(usuario: str, clave: str, a: Ajustes) -> bool:
    """True solo si el usuario existe y la contraseña coincide.

    El camino es el mismo exista o no el usuario: siempre se corre un `verify`
    de argon2, contra el hash real o contra el señuelo.
    """
    usuarios = cargar_usuarios(a)
    hash_guardado = usuarios.get(usuario, _HASH_SENUELO)
    try:
        _hasher.verify(hash_guardado, clave)
    except Argon2Error:
        return False
    return usuario in usuarios


def obtener_secreto_sesion(a: Ajustes) -> str:
    """Secreto de firma: entorno > archivo > generado y persistido.

    Cambiar el secreto invalida todas las sesiones abiertas, asi que una vez
    generado se conserva. En un despliegue real viene por entorno y este archivo
    no se usa.
    """
    del_entorno = os.environ.get("STUDIOCUTTER_SECRET", "").strip()
    if len(del_entorno) >= LARGO_MINIMO_SECRETO:
        return del_entorno
    if del_entorno:
        raise ErrorDeConfiguracion(
            f"STUDIOCUTTER_SECRET tiene {len(del_entorno)} caracteres; "
            f"hacen falta al menos {LARGO_MINIMO_SECRETO}"
        )

    if a.archivo_secreto.is_file():
        guardado = a.archivo_secreto.read_text(encoding="utf-8").strip()
        if len(guardado) >= LARGO_MINIMO_SECRETO:
            return guardado

    return _generar_y_persistir(a.archivo_secreto)


def _generar_y_persistir(destino: Path) -> str:
    secreto = secrets.token_urlsafe(64)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(secreto + "\n", encoding="utf-8")
    return secreto
