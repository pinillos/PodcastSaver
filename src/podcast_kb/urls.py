"""Normalización de URLs de enclosure.

El hash de la URL normalizada es la clave de deduplicación global (§4.2 del
diseño): es lo único que detecta el mismo audio publicado en dos feeds
distintos, como los episodios de La Tertul-IA que salen también dentro de
Growth (§2.2).

Para que sirva, la normalización tiene que quitar dos cosas que cambian sin
que cambie el audio: los prefijos de tracking que anteponen los hostings, y
los parámetros de query de analítica.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit, urlunsplit

# Hosts que actúan como redirectores/medidores por delante del audio real.
TRACKER_HOSTS = frozenset({
    "chrt.fm",
    "chtbl.com",
    "claritaspod.com",
    "dts.podtrac.com",
    "mgln.ai",
    "pdrl.fm",
    "pdst.fm",
    "podtrac.com",
    "prfx.byspotify.com",
    "sphinx.acast.com",
    "traffic.megaphone.fm.pdst.fm",
    "verifi.podscribe.com",
    "www.podtrac.com",
})

# Segmentos iniciales de path que los trackers anteponen antes de la URL real.
_TRACKER_PATH_PREFIXES = (
    re.compile(r"^/pts/redirect\.[a-z0-9]+/", re.I),
    re.compile(r"^/redirect\.[a-z0-9]+/", re.I),
    re.compile(r"^/track/[^/]+/", re.I),
    re.compile(r"^/e/", re.I),
    re.compile(r"^/rss/p/", re.I),
    re.compile(r"^/[a-z0-9]+/", re.I),  # último recurso: un id opaco
)

_MAX_UNWRAPS = 6


def _unwrap_once(url: str) -> str | None:
    """Quita una capa de tracking. Devuelve None si no había ninguna."""
    parts = urlsplit(url)
    if parts.hostname is None or parts.hostname.lower() not in TRACKER_HOSTS:
        return None

    path = parts.path
    for pattern in _TRACKER_PATH_PREFIXES:
        stripped = pattern.sub("", path, count=1)
        if stripped == path:
            continue
        if not stripped:
            continue
        # El resto puede venir con esquema propio o sin él.
        if stripped.startswith(("http://", "https://")):
            return stripped
        if "/" in stripped and "." in stripped.split("/", 1)[0]:
            return "https://" + stripped
    return None


def unwrap_trackers(url: str) -> str:
    """Devuelve la URL real que hay detrás de los prefijos de tracking."""
    current = url
    for _ in range(_MAX_UNWRAPS):
        nxt = _unwrap_once(current)
        if nxt is None:
            return current
        current = nxt
    return current


def normalize_enclosure_url(url: str) -> str:
    """URL canónica de un enclosure, estable frente a tracking y analítica.

    No sirve para descargar — para eso se usa la URL original, que es la que
    el hosting espera. Sirve solo como identidad.
    """
    url = unwrap_trackers(url.strip())
    parts = urlsplit(url)

    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if parts.port and not (
        (scheme == "https" and parts.port == 443) or (scheme == "http" and parts.port == 80)
    ):
        host = f"{host}:{parts.port}"

    # La query de un enclosure es casi siempre analítica y cambia sola.
    # El fragmento nunca identifica al fichero.
    return urlunsplit((scheme, host, parts.path, "", ""))


def enclosure_sha256(url: str) -> str:
    """Clave de dedupe global (§4.2)."""
    return hashlib.sha256(normalize_enclosure_url(url).encode("utf-8")).hexdigest()
