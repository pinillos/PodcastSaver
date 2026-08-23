"""Resolución de rutas de configuración.

Los ficheros de `config/` se resuelven contra la raíz del proyecto, no contra
el `cwd`. Con rutas relativas al `cwd`, ejecutar el CLI desde otro directorio
hace que el glosario y los fixups no se carguen **y no se note**: la
transcripción sale peor sin un solo mensaje de error.
"""

from __future__ import annotations

import os
from pathlib import Path

_MARKERS = ("pyproject.toml", "config")


def project_root(start: Path | None = None) -> Path:
    """Raíz del proyecto: el ancestro que contiene pyproject.toml y config/.

    `PODCAST_KB_ROOT` la fuerza, para despliegues donde el paquete no vive
    junto a su configuración.
    """
    override = os.environ.get("PODCAST_KB_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    here = (start or Path(__file__)).resolve()
    for candidate in [here, *here.parents]:
        if all((candidate / marker).exists() for marker in _MARKERS):
            return candidate

    cwd = Path.cwd()
    if all((cwd / marker).exists() for marker in _MARKERS):
        return cwd
    return cwd


def config_path(*parts: str) -> Path:
    return project_root().joinpath("config", *parts)
