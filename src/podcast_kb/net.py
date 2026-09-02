"""Descargas seguras de URLs que vienen de terceros.

Todo lo que este sistema descarga —el feed, el audio, los subtítulos— sale
de un RSS remoto. Un feed comprometido, o simplemente uno hostil que alguien
dé de alta, puede apuntar a donde quiera: al servicio de metadatos de la nube
si el indexado corre en un runner, a `localhost` si corre en el portátil, o a
un fichero de 100 GB para agotar el disco.

Aquí se valida el destino y se acota el tamaño. Las redirecciones se siguen a
mano porque validar solo la primera URL no sirve de nada: basta redirigir.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

import httpx

# Escape para desarrollo y tests: permite apuntar a un servidor local. Está
# apagado por defecto a propósito; encenderlo en producción reabre el SSRF.
PERMITIR_PRIVADAS_POR_DEFECTO = os.environ.get("PODCAST_KB_ALLOW_PRIVATE_URLS") == "1"

MAX_REDIRECCIONES = 5

# Topes por tipo de recurso. Generosos, pero acotados.
MAX_FEED_BYTES = 32 * 1024 * 1024
MAX_SUBTITULO_BYTES = 16 * 1024 * 1024
MAX_AUDIO_BYTES = 1024 * 1024 * 1024


class UrlNoPermitida(ValueError):
    """El destino no es una dirección pública alcanzable."""


class DemasiadoGrande(ValueError):
    """La respuesta supera el tope permitido."""


def _es_publica(ip: str) -> bool:
    direccion = ipaddress.ip_address(ip)
    return not (
        direccion.is_private
        or direccion.is_loopback
        or direccion.is_link_local
        or direccion.is_reserved
        or direccion.is_multicast
        or direccion.is_unspecified
    )


def validar_url(url: str, *, permitir_privadas: bool | None = None) -> None:
    """Rechaza esquemas raros y destinos no públicos."""
    if permitir_privadas is None:
        permitir_privadas = PERMITIR_PRIVADAS_POR_DEFECTO
    partes = urlsplit(url)
    if partes.scheme not in ("http", "https"):
        raise UrlNoPermitida(f"esquema no permitido: {partes.scheme!r} en {url[:80]}")
    if not partes.hostname:
        raise UrlNoPermitida(f"URL sin host: {url[:80]}")
    if permitir_privadas:
        return

    try:
        infos = socket.getaddrinfo(partes.hostname, partes.port or 0, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UrlNoPermitida(f"no se resuelve {partes.hostname}: {exc}") from exc

    for info in infos:
        ip = info[4][0]
        if not _es_publica(ip):
            raise UrlNoPermitida(
                f"{partes.hostname} resuelve a {ip}, que no es una dirección pública. "
                "Un feed no debería poder hacernos pedir esto."
            )


def get_validado(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    permitir_privadas: bool | None = None,
) -> httpx.Response:
    """GET siguiendo redirecciones a mano, validando cada salto.

    `client` debe tener `follow_redirects=False`: seguirlas automáticamente
    saltaría la validación en el primer redirect.
    """
    actual = url
    for _ in range(MAX_REDIRECCIONES + 1):
        validar_url(actual, permitir_privadas=permitir_privadas)
        respuesta = client.get(actual, headers=headers, follow_redirects=False)
        if respuesta.is_redirect and respuesta.has_redirect_location:
            actual = str(respuesta.next_request.url)
            respuesta.close()
            continue
        return respuesta
    raise UrlNoPermitida(f"demasiadas redirecciones desde {url[:80]}")


def leer_acotado(respuesta: httpx.Response, tope: int, *, que: str = "la respuesta") -> bytes:
    """Lee el cuerpo sin pasar del tope, aunque el servidor mienta en Content-Length."""
    declarado = respuesta.headers.get("Content-Length")
    if declarado and declarado.isdigit() and int(declarado) > tope:
        raise DemasiadoGrande(f"{que} declara {int(declarado):,} bytes (tope {tope:,})")

    trozos: list[bytes] = []
    total = 0
    for trozo in respuesta.iter_bytes(chunk_size=1 << 16):
        total += len(trozo)
        if total > tope:
            respuesta.close()
            raise DemasiadoGrande(f"{que} supera el tope de {tope:,} bytes")
        trozos.append(trozo)
    return b"".join(trozos)
