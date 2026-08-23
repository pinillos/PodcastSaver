"""config/podcasts.yaml: fuente de verdad del alta de podcasts (§4.1).

SQLite es caché derivada. Si divergen, gana el YAML.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .paths import config_path

DEFAULT_CONFIG_PATH = config_path("podcasts.yaml")

_REQUIRED = ("slug", "language")


class ConfigError(RuntimeError):
    pass


def load_podcasts(path: Path | str = DEFAULT_CONFIG_PATH) -> list[dict]:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"No existe {path}. Créalo o usa `podcast-kb add`.")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("podcasts")
    if not isinstance(entries, list):
        raise ConfigError(f"{path} debe contener una lista bajo la clave `podcasts`.")

    slugs: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError(f"Entrada inválida en {path}: {entry!r}")
        for key in _REQUIRED:
            if not entry.get(key):
                raise ConfigError(f"Falta `{key}` en la entrada {entry.get('slug', entry)!r}")
        if not entry.get("rss_url") and not entry.get("apple_id"):
            raise ConfigError(
                f"`{entry['slug']}` necesita `rss_url` o `apple_id` para poder resolverse."
            )
        if entry["slug"] in slugs:
            raise ConfigError(f"slug duplicado en {path}: {entry['slug']!r}")
        slugs.add(entry["slug"])

    return entries


def save_podcasts(entries: list[dict], path: Path | str = DEFAULT_CONFIG_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"podcasts": entries}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
