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
import threading
from pathlib import Path

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import Argon2Error, InvalidHashError

from .config import Ajustes, ErrorDeConfiguracion

LARGO_MINIMO_SECRETO = 32

_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
"""Perfil de **baja memoria** de la RFC 9106 (19 MiB, t=2, p=1), no los defaults.

Los defaults de argon2-cffi son `memory_cost=65536` KiB —**64 MiB por hash**— con
`parallelism=4`. En una instancia de 512 MB eso es caro dos veces: una al importar
este modulo (el señuelo de abajo) y otra **por cada verify**, que al correr en el
threadpool se multiplica por los pedidos en vuelo. Con 0,1 CPU, ademas,
`parallelism=4` solo agrega contencion: no hay cuatro nucleos que repartir.

El perfil de la RFC 9106 §4 para entornos con poca memoria mantiene la dureza
por un camino distinto —mas pasadas sobre menos memoria— y es el recomendado
justamente para este caso.

⚠ **No invalida ninguna credencial existente.** Los parametros viajan dentro del
propio string del hash (`$argon2id$v=19$m=...,t=...,p=...$`), asi que `verify`
usa los del hash guardado, no los del hasher. Un `credenciales.json` viejo sigue
funcionando; los hashes nuevos salen con estos parametros (ver `DESPLIEGUE.md` §2.4).
"""

_HASH_SENUELO = _hasher.hash(secrets.token_urlsafe(32))
"""Hash contra el que se verifica cuando el usuario no existe.

Se calcula una vez al importar el modulo, sobre un valor aleatorio que nadie
conoce: verificar contra el siempre falla, pero **cuesta lo mismo** que
verificar contra un hash real. Es lo que hace que el login no revele si un
usuario existe.

Es el señuelo de ultimo recurso: el que se usa de verdad lo arma
`_senuelo_como()` con los parametros de los hashes guardados.
"""

_senuelos: dict[str, str] = {}
_candado_senuelos = threading.Lock()


def _senuelo_como(muestra: str | None) -> str:
    """Señuelo con los MISMOS parametros de argon2 que `muestra`.

    Sin esto, el señuelo deja de cumplir su funcion apenas los parametros del
    modulo y los del archivo de credenciales no coinciden — que es justo lo que
    pasa al bajar el `memory_cost` de 64 MiB a 19: verificar contra un señuelo
    barato termina **antes** que verificar contra un hash guardado caro, y esa
    diferencia de tiempo es exactamente el enumerador de usuarios que el señuelo
    existe para tapar. Los parametros los decide el `credenciales.json`, no este
    modulo, asi que el señuelo los tiene que copiar de ahi.

    Se cachea por juego de parametros: el hash caro se paga una vez por proceso.
    """
    if muestra is None:
        return _HASH_SENUELO
    try:
        parametros = extract_parameters(muestra)
    except InvalidHashError:
        return _HASH_SENUELO
    clave = str(parametros)
    with _candado_senuelos:
        senuelo = _senuelos.get(clave)
        if senuelo is None:
            senuelo = PasswordHasher.from_parameters(parametros).hash(secrets.token_urlsafe(32))
            _senuelos[clave] = senuelo
    return senuelo


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


def hashear(clave: str) -> str:
    """Hash argon2id de una contraseña, con los parametros de esta app.

    Existe para que dar de alta un usuario (`DESPLIEGUE.md` §2.4) no exija
    repetir los parametros en la linea de comandos: escritos en dos lados, el
    dia que cambien aca los hashes nuevos saldrian con los viejos y nadie se
    enteraria. La contraseña en claro entra y no sale: no se loguea ni se guarda.
    """
    return _hasher.hash(clave)


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
    # El señuelo se arma SIEMPRE, exista o no el usuario: si solo se armara en
    # la rama del usuario inexistente, la primera vez costaria un hash de mas y
    # esa asimetria seria, otra vez, medible desde afuera.
    senuelo = _senuelo_como(next(iter(usuarios.values()), None))
    hash_guardado = usuarios.get(usuario, senuelo)
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
