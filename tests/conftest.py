"""Andamiaje de los tests de la capa web.

Dos decisiones que hacen que estos tests sirvan de red y no de decorado:

- **Nada toca el estado real.** El directorio de trabajos, el almacen y el
  archivo de credenciales se reemplazan por copias de `tmp_path` via
  `dependency_overrides`. Un test no puede ensuciar `trabajo/` ni depender de
  que `credenciales.json` exista en la maquina donde corre (esta gitignoreado,
  asi que en un clon limpio no esta).
- **La contraseña de prueba se genera al vuelo.** Nunca hay una credencial
  literal en el repo: seria un secreto commiteado aunque sea de mentira, y el
  gate de secretos tendria razon en marcarlo.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from PIL import Image

from app.almacen import AlmacenEnMemoria
from app.config import Ajustes
from app.dependencias import obtener_ajustes, obtener_almacen
from app.main import app
from app.proteccion import reiniciar_frenos
from app.trabajos import detener_todo

USUARIO = "tester"
CLAVE = secrets.token_urlsafe(16)
"""Se genera en cada corrida: no hay ninguna contraseña escrita en el repo."""


@pytest.fixture(autouse=True)
def frenos_limpios() -> Iterator[None]:
    """Los frenos por IP arrancan vacios en cada test.

    Son estado de modulo —tienen que serlo: el middleware no recibe fixtures— y
    todos los tests salen de la misma IP (`testclient`). Sin este reinicio, los
    intentos fallidos de un test de login se le suman al siguiente y el orden de
    ejecucion decidiria quien pasa: la clase de test intermitente que despues
    nadie puede reproducir.
    """
    reiniciar_frenos()
    yield
    reiniciar_frenos()


@pytest.fixture(autouse=True)
def trabajos_limpios() -> Iterator[None]:
    """Ningun test le deja un proceso —ni un lugar del cupo— al siguiente.

    Mismo problema y misma forma que `frenos_limpios`: el registro de procesos y
    de series vive en el modulo, y `max_trabajos_simultaneos` es 3. Tres tests
    que lanzan un trabajo y no lo esperan hacen que el cuarto reciba un 429
    **segun el orden en que corran**.

    Va despues del test y no antes para que el que lo necesite pueda mirar el
    proceso mientras vive (`proceso_de`), que es lo que hacen los del timeout.
    """
    yield
    detener_todo()


@pytest.fixture
def ajustes(tmp_path: Path) -> Ajustes:
    credenciales = tmp_path / "credenciales.json"
    credenciales.write_text(
        json.dumps({"usuarios": [{"usuario": USUARIO, "hash": PasswordHasher().hash(CLAVE)}]}),
        encoding="utf-8",
    )
    # El timeout queda en el valor de produccion: el test que lo prueba se hace
    # su propia copia impaciente con `dataclasses.replace`. Acortarlo para todos
    # haria fallar justo al unico test que corre el motor de verdad.
    return Ajustes(
        dir_trabajo=tmp_path / "trabajo",
        archivo_credenciales=credenciales,
        archivo_secreto=tmp_path / "sesion.key",
    )


@pytest.fixture
def almacen() -> AlmacenEnMemoria:
    return AlmacenEnMemoria()


@pytest.fixture
def cliente(ajustes: Ajustes, almacen: AlmacenEnMemoria) -> Iterator[TestClient]:
    """Cliente sin sesion. Sirve para probar lo que pasa sin autenticar."""
    app.dependency_overrides[obtener_ajustes] = lambda: ajustes
    app.dependency_overrides[obtener_almacen] = lambda: almacen
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def sesion(cliente: TestClient) -> TestClient:
    """Cliente con la sesion ya iniciada."""
    respuesta = cliente.post("/login", data={"usuario": USUARIO, "clave": CLAVE})
    assert respuesta.status_code == 303, "el login del andamiaje tiene que funcionar"
    return cliente


@pytest.fixture
def png_minimo() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (60, 40), (255, 255, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def jpg_minimo() -> bytes:
    buffer = BytesIO()
    imagen = Image.new("RGB", (120, 80), (255, 255, 255))
    for x in range(20, 100):
        for y in range(35, 45):
            imagen.putpixel((x, y), (0, 0, 0))
    imagen.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()
