"""studioCutter3D — capa web.

Orquesta el motor `cutter3d`; **no reimplementa nada de geometria**. Esa frontera
es la que permite testear el motor aislado y la que va a permitir, el dia que
esto sea un servicio, cambiar la capa de arriba sin tocar lo de abajo.
"""

from __future__ import annotations

__version__ = "1.0.2"
