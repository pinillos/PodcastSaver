"""Descargas seguras de URLs que vienen de terceros.

Todo lo que este sistema descarga —el feed, el audio, los subtítulos— sale
de un RSS remoto. Un feed comprometido, o simplemente uno hostil que alguien
dé de alta, puede apuntar a donde quiera: al servicio de metadatos de la nube
si el indexado corre en un runner, a `localhost` si corre en el portátil, o a
un fichero de 100 GB para agotar el disco.

Aquí se valida el destino y se acota el tamaño. Tres detalles que no son
opcionales:

1. Las redirecciones se siguen a mano, validando cada salto: validar solo la
   primera URL no sirve de nada, basta redirigir.
2. La IP que se valida es la IP a la que se conecta. Resolver para comprobar
   y dejar que el cliente resuelva otra vez al conectar deja una ventana
   (DNS rebinding): un DNS hostil puede contestar una IP pública a la
   comprobación y `127.0.0.1` a la conexión. Se fija la IP validada y se
   mandan el `Host` y el SNI originales, así que el certificado se sigue
   verificando contra el nombre real.
3. El cuerpo se lee en streaming. Un tope que se comprueba después de que
   httpx se haya tragado la respuesta entera en memoria no acota nada.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

# Escape para desarrollo y tests: permite apuntar a un servidor local. Está
# apagado por defecto a propósito; encenderlo en producción reabre el SSRF.
PERMITIR_PRIVADAS_POR_DEFECTO = os.environ.get("PODCAST_KB_ALLOW_PRIVATE_URLS") == "1"

MAX_REDIRECCIONES = 5

# Topes por tipo de recurso. Generosos, pero acotados.
MAX_FEED_BYTES = 32 * 1024 * 1024
MAX_SUBTITULO_BYTES = 16 * 1024 * 1024
MAX_AUDIO_BYTES = 1024 * 1024 * 1024

_VARIABLES_PROXY = (
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
)


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


def validar_url(url: str, *, permitir_privadas: bool | None = None) -> list[str]:
    """Rechaza esquemas raros y destinos no públicos.

    Devuelve las IPs a las que resuelve el host, ya validadas, para que quien
    conecte use esas y no vuelva a resolver. Devuelve `[]` cuando no se ha
    validado nada (modo permisivo): no hay IP que fijar.
    """
    if permitir_privadas is None:
        permitir_privadas = PERMITIR_PRIVADAS_POR_DEFECTO
    partes = urlsplit(url)
    if partes.scheme not in ("http", "https"):
        raise UrlNoPermitida(f"esquema no permitido: {partes.scheme!r} en {url[:80]}")
    if not partes.hostname:
        raise UrlNoPermitida(f"URL sin host: {url[:80]}")
    if permitir_privadas:
        return []

    try:
        infos = socket.getaddrinfo(partes.hostname, partes.port or 0, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UrlNoPermitida(f"no se resuelve {partes.hostname}: {exc}") from exc

    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if not _es_publica(ip):
            raise UrlNoPermitida(
                f"{partes.hostname} resuelve a {ip}, que no es una dirección pública. "
                "Un feed no debería poder hacernos pedir esto."
            )
        if ip not in ips:
            ips.append(ip)
    return ips


def hay_proxy() -> bool:
    """¿Sale el tráfico por un proxy?

    Si lo hay, quien resuelve el nombre es el proxy: fijar la IP local no
    protege de nada y además rompería el CONNECT. Se documenta como límite:
    detrás de un proxy, la defensa contra SSRF es la del proxy.
    """
    return any(os.environ.get(nombre) for nombre in _VARIABLES_PROXY)


def _fijar_ip_activo() -> bool:
    if os.environ.get("PODCAST_KB_PIN_DNS") == "0":
        return False
    return not hay_proxy()


def _peticion_fijada(
    client: httpx.Client, url: str, ip: str, headers: dict[str, str] | None
) -> httpx.Request:
    """Petición idéntica pero conectando a `ip`, con Host y SNI originales."""
    partes = urlsplit(url)
    credenciales, _, autoridad = partes.netloc.rpartition("@")
    literal = f"[{ip}]" if ":" in ip else ip
    netloc = literal if partes.port is None else f"{literal}:{partes.port}"
    if credenciales:
        # Hay feeds privados con usuario y contraseña en la URL: si se pierden
        # aquí, el servidor contesta 401 y parece que el feed se ha caído.
        netloc = f"{credenciales}@{netloc}"
    url_ip = urlunsplit((partes.scheme, netloc, partes.path, partes.query, ""))

    cabeceras = dict(headers or {})
    cabeceras["Host"] = autoridad
    return client.build_request(
        "GET", url_ip, headers=cabeceras,
        extensions={"sni_hostname": partes.hostname},
    )


def url_final(respuesta: httpx.Response) -> str:
    """URL lógica de la respuesta, la que hay que persistir.

    Con la IP fijada, `respuesta.url` lleva la IP; lo que importa —y lo que
    sirve de base para resolver enclosures relativos— es el nombre.
    """
    return str(respuesta.extensions.get("podcast_kb_url") or respuesta.url)


def get_validado(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    permitir_privadas: bool | None = None,
) -> httpx.Response:
    """GET siguiendo redirecciones a mano, validando cada salto.

    Devuelve la respuesta **sin leer**: el cuerpo se consume en streaming con
    `leer_acotado` o `iter_bytes`, y hay que cerrarla. `client` debe tener
    `follow_redirects=False`: seguirlas automáticamente saltaría la
    validación en el primer redirect.
    """
    actual = url
    for _ in range(MAX_REDIRECCIONES + 1):
        ips = validar_url(actual, permitir_privadas=permitir_privadas)
        if ips and _fijar_ip_activo():
            peticion = _peticion_fijada(client, actual, ips[0], headers)
        else:
            peticion = client.build_request("GET", actual, headers=headers)

        respuesta = client.send(peticion, stream=True, follow_redirects=False)
        respuesta.extensions["podcast_kb_url"] = actual

        destino = respuesta.headers.get("Location")
        if respuesta.is_redirect and destino:
            # Relativa a la URL lógica, no a la que lleva la IP.
            actual = urljoin(actual, destino)
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
