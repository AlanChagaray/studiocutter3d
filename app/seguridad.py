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


VARIABLE_CREDENCIALES = "STUDIOCUTTER_CREDENCIALES_JSON"
"""Las credenciales completas, en JSON, adentro de una variable de entorno.

Existe por el contenedor. `credenciales.json` esta gitignoreado —y tiene que
seguir estandolo—, asi que una imagen construida desde el repo no lo trae y una
imagen que lo trajera seria peor: los hashes quedarian en una capa, y cualquiera
con acceso al registro se los lleva. Las dos formas sanas de que el secreto
llegue al contenedor sin pasar por la imagen son montar el archivo (lo que hace
`docker-compose` y lo que hacen los *secret files* de Render) o esta variable,
para las plataformas donde montar un archivo no es practico.

**Si estan las dos fuentes gana la variable**, que es la que se configura en el
panel de la plataforma: tiene que poder corregir un montaje viejo sin
reconstruir nada. Borrarla devuelve el control al archivo.
"""


def cargar_usuarios(a: Ajustes) -> dict[str, str]:
    """Devuelve {usuario: hash}. Primero la variable de entorno, si no el archivo."""
    crudo = os.environ.get(VARIABLE_CREDENCIALES, "").strip()
    if crudo:
        return _parsear(crudo, origen=VARIABLE_CREDENCIALES)
    if not a.archivo_credenciales.is_file():
        raise ErrorDeConfiguracion(
            f"falta {a.archivo_credenciales.name} y {VARIABLE_CREDENCIALES} esta vacia: "
            "no hay ningun usuario dado de alta"
        )
    return _parsear(
        a.archivo_credenciales.read_text(encoding="utf-8"), origen=a.archivo_credenciales.name
    )


def _parsear(crudo: str, *, origen: str) -> dict[str, str]:
    """JSON de credenciales a {usuario: hash}, venga de donde venga.

    El mensaje de error nombra el origen —archivo o variable— porque con dos
    fuentes posibles, "el formato no es el esperado" sin decir de que no alcanza
    para saber donde mirar.
    """
    try:
        entradas = json.loads(crudo)["usuarios"]
        return {str(e["usuario"]): str(e["hash"]) for e in entradas}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ErrorDeConfiguracion(
            f"{origen} no tiene el formato esperado "
            '({"usuarios": [{"usuario": ..., "hash": ...}]})'
        ) from exc


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
