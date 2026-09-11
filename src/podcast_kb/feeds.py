"""Ingesta de feeds RSS (§4 del diseño).

Dos responsabilidades:
  - resolver el feed real a partir de un ID de Apple Podcasts (§2.3);
  - descargar y parsear el feed, extrayendo también los elementos de
    Podcasting 2.0 que feedparser no expone (§4.4).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import feedparser
import httpx

from . import net
from .chapters import extract_chapters
from .urls import enclosure_sha256, resolve_relative

USER_AGENT = "podcast-kb/0.1 (+base de conocimiento personal; contacto en el repo)"

ITUNES_LOOKUP = "https://itunes.apple.com/lookup"

# El espacio de nombres de Podcasting 2.0 circula con dos URIs distintas; los
# feeds a mano usan indistintamente una u otra.
PODCAST_NS = (
    "https://podcastindex.org/namespace/1.0",
    "https://podcastindex.org/namespace/1.0/",
    "https://github.com/Podcastindex-org/podcast-namespace/blob/main/docs/1.0.md",
)
PSC_NS = "http://podlove.org/simple-chapters"
MEDIA_NS = "http://search.yahoo.com/mrss/"

# Un item puede declarar el mismo texto en varios formatos. Los que llevan
# marcas de tiempo valen infinitamente más: sin ellas no hay salto al minuto,
# que es el valor central del sistema (§5.12). El texto plano sirve de poco
# porque habría que realinearlo, así que se queda el último.
_TRANSCRIPT_PREFERENCE = (
    "text/vtt",
    "application/x-subrip",
    "text/srt",
    "application/srt",
    "application/json",
    "application/x-json+chapters",
    "text/html",
    "plain/txt",
    "text/plain",
)
_TIMESTAMPED_TYPES = frozenset({
    "text/vtt", "application/x-subrip", "text/srt", "application/srt", "application/json",
})
ATOM_NS = "http://www.w3.org/2005/Atom"

# Solo con marcador explícito. Un número suelto en el título casi nunca es el
# episodio: en un podcast de IA es la versión de un modelo ("Llama 4",
# "Veo 3", "Gemini 3"), y colarlo como episode_number contamina el front
# matter y el título del .md. Es preferible no tener número a tener uno falso.
_EP_NUM_RE = re.compile(
    r"""(?xi)
    (?: \#\s*(?P<hash>\d{1,4})
      | \bep (?:isodio|isode)? \.?\s* (?P<ep>\d{1,4})
      ) \b
    """
)

# pod.link y Apple Podcasts comparten el mismo identificador numérico, así que
# ambas URLs valen como entrada y no hay que hacer que el usuario lo extraiga.
_APPLE_ID_RE = re.compile(
    r"""(?x)
    ^(?P<bare>\d{6,12})$
    | pod\.link/(?P<podlink>\d{6,12})
    | podcasts\.apple\.com/(?:[^/]+/)?podcast/(?:[^/]+/)?id(?P<apple>\d{6,12})
    | /id(?P<idform>\d{6,12})
    """,
    re.I,
)


class FeedError(RuntimeError):
    pass


@dataclass
class EpisodeItem:
    guid: str
    title: str
    published_at: str
    audio_url: str
    enclosure_sha256: str
    duration_sec: int | None = None
    audio_bytes: int | None = None
    description: str | None = None
    episode_url: str | None = None
    episode_number: int | None = None
    feed_transcript_url: str | None = None
    feed_transcript_type: str | None = None
    feed_chapters_url: str | None = None
    chapters: list[dict] = field(default_factory=list)
    chapters_source: str = "none"
    persons: list[str] = field(default_factory=list)

    @property
    def transcript_has_timestamps(self) -> bool:
        """Sin marcas de tiempo la transcripción del feed no nos sirve sola."""
        return (self.feed_transcript_type or "").lower() in _TIMESTAMPED_TYPES


@dataclass
class FeedInspection:
    """Diagnóstico de un feed antes de darlo por bueno (§2.3)."""

    rss_url: str
    final_url: str | None = None
    status: int | None = None
    ok: bool = False
    error: str | None = None
    title: str | None = None
    language: str | None = None
    n_items: int = 0
    first_published: str | None = None
    last_published: str | None = None
    n_transcripts: int = 0
    n_transcripts_timed: int = 0
    n_chapters: int = 0
    n_note_chapters: int = 0
    self_link: str | None = None
    trackers: list[str] = field(default_factory=list)


@dataclass
class FeedResult:
    title: str | None
    language: str | None
    items: list[EpisodeItem] = field(default_factory=list)
    self_link: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


def extract_apple_id(value: str) -> str:
    """Acepta un ID suelto o una URL de pod.link / Apple Podcasts.

    Los tres identifican el mismo podcast con el mismo número, así que pedirle
    al usuario que lo recorte a mano solo añade una forma de equivocarse.
    """
    value = (value or "").strip()
    match = _APPLE_ID_RE.search(value)
    if not match:
        raise FeedError(
            f"No encuentro un ID de Apple Podcasts en {value!r}. "
            "Vale el número suelto, una URL de pod.link o una de podcasts.apple.com."
        )
    return next(g for g in match.groups() if g)


def resolve_feed_from_apple_id(apple_id: str, *, client: httpx.Client | None = None) -> dict:
    """Resuelve feedUrl y metadatos desde un ID de Apple Podcasts (§2.3)."""
    apple_id = extract_apple_id(str(apple_id))
    owns_client = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30)
    try:
        resp = client.get(ITUNES_LOOKUP, params={"id": apple_id, "entity": "podcast"})
        resp.raise_for_status()
        payload = resp.json()
    finally:
        if owns_client:
            client.close()

    results = payload.get("results") or []
    if not results:
        raise FeedError(f"Apple no devuelve resultados para el id {apple_id}")

    entry = results[0]
    if not entry.get("feedUrl"):
        raise FeedError(
            f"El podcast {entry.get('collectionName')!r} no expone feedUrl. "
            "Puede ser exclusivo de plataforma."
        )
    return {
        "apple_id": str(apple_id),
        "rss_url": entry["feedUrl"],
        "title": entry.get("collectionName"),
        "authors": entry.get("artistName"),
        "website": entry.get("collectionViewUrl"),
    }


def fetch_feed(
    rss_url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    client: httpx.Client | None = None,
    allow_private: bool | None = None,
) -> tuple[bytes | None, str | None, str | None, str]:
    """Descarga el feed usando caché condicional (§4.2).

    Devuelve (contenido, etag, last_modified, url_final). El contenido es None
    si el servidor responde 304: no hay nada nuevo que parsear. La URL final es
    la que hay tras las redirecciones, y es la que sirve de base para resolver
    enclosures relativos.
    """
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    owns_client = client is None
    # follow_redirects=False a propósito: las sigue net.get_validado
    # validando cada salto (si no, basta un redirect para saltarse el filtro).
    client = client or httpx.Client(timeout=60, follow_redirects=False)
    try:
        resp = net.get_validado(client, rss_url, headers=headers,
                                permitir_privadas=allow_private)
        # §12: respetar el rate limiting que pida el servidor.
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "?")
            raise FeedError(f"429 del servidor; Retry-After={retry_after}")
        final_url = str(resp.url)
        if resp.status_code == 304:
            return None, etag, last_modified, final_url
        resp.raise_for_status()
        contenido = net.leer_acotado(resp, net.MAX_FEED_BYTES, que="el feed")
        return (
            contenido,
            resp.headers.get("ETag"),
            resp.headers.get("Last-Modified"),
            final_url,
        )
    except (net.UrlNoPermitida, net.DemasiadoGrande) as exc:
        raise FeedError(str(exc)) from exc
    finally:
        if owns_client:
            client.close()


def _to_iso(struct_time: Any) -> str | None:
    if not struct_time:
        return None
    try:
        dt = datetime(*struct_time[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return dt.isoformat(timespec="seconds")


def parse_duration(raw: Any) -> int | None:
    """`<itunes:duration>` viene como segundos, MM:SS o HH:MM:SS."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if ":" not in text:
        try:
            return int(float(text))
        except ValueError:
            return None
    parts = text.split(":")
    if len(parts) > 3:
        return None
    try:
        nums = [int(float(p)) for p in parts]
    except ValueError:
        return None
    total = 0
    for n in nums:
        total = total * 60 + n
    return total


def extract_episode_number(title: str, entry: Any = None) -> int | None:
    """`<itunes:episode>` si existe; si no, un número en el título."""
    if entry is not None:
        raw = getattr(entry, "get", lambda *_: None)("itunes_episode")
        if raw:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    match = _EP_NUM_RE.search(title or "")
    if not match:
        return None
    return int(match.group("hash") or match.group("ep"))


def _find_all_ns(item: ET.Element, tag: str) -> list[ET.Element]:
    """Todos los elementos con ese tag, en cualquiera de las URIs conocidas."""
    found: list[ET.Element] = []
    for ns in PODCAST_NS:
        found.extend(item.findall(f"{{{ns}}}{tag}"))
    return found


def _rank_transcript(element: ET.Element) -> int:
    mimetype = (element.get("type") or "").lower()
    try:
        return _TRANSCRIPT_PREFERENCE.index(mimetype)
    except ValueError:
        return len(_TRANSCRIPT_PREFERENCE)


def _pick_transcript(item: ET.Element) -> tuple[str | None, str | None]:
    """Elige el mejor formato de transcripción declarado por el feed.

    Ojo: `find()` devolvería el primero del documento, que en Cuonda es
    `plain/txt` — sin timestamps. Hay que ordenar por preferencia.
    """
    candidatos = [t for t in _find_all_ns(item, "transcript") if t.get("url")]
    if not candidatos:
        return None, None
    best = min(candidatos, key=_rank_transcript)
    return best.get("url"), best.get("type")


def _podcasting20_by_index(raw_xml: bytes) -> list[dict]:
    """Elementos <podcast:*> por POSICIÓN del item (§4.4).

    Indexar por `guid` parecía natural, pero los feeds autoalojados a menudo
    omiten el guid — y entonces la transcripción publicada se perdía en
    silencio, que es justo lo contrario de lo que queremos. feedparser conserva
    el orden de los items, así que la posición es una clave más fiable.
    """
    found: list[dict] = []
    try:
        # El BOM rompe ET.fromstring; feedparser lo tolera, así que sin esto
        # los dos parseos se desalineaban.
        root = ET.fromstring(raw_xml.lstrip(b"\xef\xbb\xbf").lstrip())
    except ET.ParseError:
        return found

    for item in root.iter("item"):
        data: dict[str, Any] = {}

        url, mimetype = _pick_transcript(item)
        if url:
            data["feed_transcript_url"] = url
            data["feed_transcript_type"] = mimetype

        chapters = next((c for c in _find_all_ns(item, "chapters") if c.get("url")), None)
        if chapters is not None:
            data["feed_chapters_url"] = chapters.get("url")
        elif item.find(f"{{{PSC_NS}}}chapters") is not None:
            data["feed_chapters_url"] = "psc:inline"

        # Quién habla, según el propio feed. Resuelve de gratis el trabajo
        # difícil de §5.11: mapear SPEAKER_XX a nombres reales.
        personas = [
            (p.text or "").strip() for p in _find_all_ns(item, "person") if (p.text or "").strip()
        ]
        if personas:
            data["persons"] = personas

        found.append(data)
    return found


def _self_link(raw_xml: bytes) -> str | None:
    """`<atom:link rel="self">`: la URL canónica que el feed declara de sí mismo."""
    try:
        root = ET.fromstring(raw_xml.lstrip(b"\xef\xbb\xbf").lstrip())
    except ET.ParseError:
        return None
    channel = root.find("channel")
    if channel is None:
        return None
    for link in channel.findall(f"{{{ATOM_NS}}}link"):
        if link.get("rel") == "self" and link.get("href"):
            return link.get("href")
    return None


def _pick_audio(entry: Any, item_el: ET.Element | None) -> tuple[str, int | None]:
    """Elige la pista de audio del item.

    Prefiere explícitamente `type="audio/*"`: un feed que publica el vídeo
    primero (habitual cuando el podcast también sale en YouTube) hacía que se
    descargara y transcribiera el .mp4.
    """
    candidatos: list[tuple[str, int | None, str]] = []
    for enclosure in entry.get("enclosures") or []:
        href = enclosure.get("href") or enclosure.get("url") or ""
        if not href:
            continue
        try:
            length = int(enclosure.get("length") or 0) or None
        except (TypeError, ValueError):
            length = None
        candidatos.append((href, length, (enclosure.get("type") or "").lower()))

    # Media RSS como alternativa: algunos feeds a mano no usan <enclosure>.
    if not candidatos and item_el is not None:
        for media in item_el.findall(f"{{{MEDIA_NS}}}content"):
            href = media.get("url") or ""
            if not href:
                continue
            try:
                length = int(media.get("fileSize") or 0) or None
            except (TypeError, ValueError):
                length = None
            candidatos.append((href, length, (media.get("type") or "").lower()))

    if not candidatos:
        return "", None
    for href, length, mimetype in candidatos:
        if mimetype.startswith("audio/"):
            return href, length
    return candidatos[0][0], candidatos[0][1]


def _clean_episode_url(raw: str | None, base: str | None) -> str | None:
    """Descarta un `link` que no sea realmente una URL.

    Por la spec de RSS, un `<guid>` sin `isPermaLink="false"` se considera
    permalink, y feedparser lo promueve a `entry.link`. Los feeds hechos a
    mano omiten ese atributo constantemente mientras usan guids que no son
    URLs (`20250107`), así que sin este filtro `episode_url` acaba siendo un
    enlace roto en el front matter y en la UI.
    """
    if not raw:
        return None
    raw = raw.strip()
    if urlsplit(raw).scheme.lower() in ("http", "https"):
        return raw
    # Una ruta absoluta del sitio sí es un enlace legítimo; un token suelto
    # sin barras (el guid promovido) no lo es. Validar DESPUÉS de resolver
    # no vale: la resolución le pondría esquema a cualquier cosa.
    if raw.startswith("/") and base:
        return resolve_relative(raw, base)
    return None


def _entry_guid(entry: Any, audio_url: str) -> str:
    """Identidad del episodio, por orden de preferencia (§4.2)."""
    guid = (entry.get("id") or "").strip()
    if guid:
        return guid
    if audio_url:
        return f"sha256:{enclosure_sha256(audio_url)}"
    title = (entry.get("title") or "").strip().lower()
    published = entry.get("published") or ""
    return f"sha256:{enclosure_sha256(title + published)}"


def parse_feed(raw_xml: bytes, base_url: str | None = None) -> FeedResult:
    parsed = feedparser.parse(raw_xml)
    extras = _podcasting20_by_index(raw_xml)
    try:
        item_els = list(ET.fromstring(raw_xml.lstrip(b"\xef\xbb\xbf").lstrip()).iter("item"))
    except ET.ParseError:
        item_els = []

    channel = parsed.feed
    result = FeedResult(
        title=channel.get("title"),
        language=(channel.get("language") or "").split("-")[0].lower() or None,
        self_link=_self_link(raw_xml),
    )
    # Los enclosures relativos se resuelven contra la URL del feed; si no la
    # tenemos, contra el <link> del canal.
    base = base_url or result.self_link or channel.get("link")

    for index, entry in enumerate(parsed.entries):
        item_el = item_els[index] if index < len(item_els) else None
        raw_audio, audio_bytes = _pick_audio(entry, item_el)
        if not raw_audio:
            continue  # sin audio no hay episodio que transcribir

        audio_url = resolve_relative(raw_audio, base)
        title = entry.get("title") or "(sin título)"
        guid = _entry_guid(entry, audio_url)
        published = _to_iso(entry.get("published_parsed")) or _to_iso(
            entry.get("updated_parsed")
        )
        if not published:
            continue  # published_at es NOT NULL: sin fecha no se da de alta

        extra = extras[index] if index < len(extras) else {}
        description = entry.get("summary")
        duration = parse_duration(entry.get("itunes_duration"))

        # §6: preferir siempre los capítulos del autor. El fichero declarado
        # con <podcast:chapters> manda; si no lo hay, valen los del índice de
        # las show notes.
        note_chapters: list[dict] = []
        chapters_source = "none"
        if extra.get("feed_chapters_url"):
            chapters_source = "feed"
        else:
            note_chapters = extract_chapters(description, duration_sec=duration)
            if note_chapters:
                chapters_source = "notes"

        result.items.append(
            EpisodeItem(
                guid=guid,
                title=title,
                published_at=published,
                audio_url=audio_url,
                enclosure_sha256=enclosure_sha256(audio_url),
                duration_sec=duration,
                audio_bytes=audio_bytes,
                description=description,
                episode_url=_clean_episode_url(entry.get("link"), base),
                episode_number=extract_episode_number(title, entry),
                feed_transcript_url=extra.get("feed_transcript_url"),
                feed_transcript_type=extra.get("feed_transcript_type"),
                feed_chapters_url=extra.get("feed_chapters_url"),
                chapters=note_chapters,
                chapters_source=chapters_source,
                persons=extra.get("persons", []),
            )
        )
    return result


def inspect_feed(
    rss_url: str, *, client: httpx.Client | None = None, allow_private: bool | None = None
) -> FeedInspection:
    """Descarga y valida un feed, sin escribir nada.

    Comprueba lo que hay que comprobar antes de dar de alta un podcast: que
    responde, cuál es la URL final tras redirecciones (§2.3: se persiste esa,
    no la inicial), que parsea, y si trae Podcasting 2.0 (§4.4), que puede
    ahorrar el backfill entero.
    """
    from .urls import TRACKER_HOSTS, unwrap_trackers

    inspection = FeedInspection(rss_url=rss_url)
    owns_client = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=True)
    try:
        resp = client.get(rss_url, headers={"User-Agent": USER_AGENT})
        inspection.status = resp.status_code
        inspection.final_url = str(resp.url)
        resp.raise_for_status()
        parsed = parse_feed(resp.content, base_url=str(resp.url))
    except httpx.HTTPError as exc:
        inspection.error = f"{type(exc).__name__}: {exc}"
        return inspection
    except Exception as exc:
        inspection.error = f"{type(exc).__name__}: {exc}"
        return inspection
    finally:
        if owns_client:
            client.close()

    inspection.ok = True
    inspection.title = parsed.title
    inspection.language = parsed.language
    inspection.n_items = len(parsed.items)
    if parsed.items:
        fechas = sorted(i.published_at for i in parsed.items)
        inspection.first_published, inspection.last_published = fechas[0], fechas[-1]
    inspection.n_transcripts = sum(1 for i in parsed.items if i.feed_transcript_url)
    inspection.n_transcripts_timed = sum(1 for i in parsed.items if i.transcript_has_timestamps)
    inspection.n_chapters = sum(1 for i in parsed.items if i.feed_chapters_url)
    inspection.n_note_chapters = sum(1 for i in parsed.items if i.chapters_source == "notes")
    inspection.self_link = parsed.self_link

    vistos: set[str] = set()
    for item in parsed.items:
        host = httpx.URL(item.audio_url).host
        if host and host.lower() in TRACKER_HOSTS:
            vistos.add(host.lower())
        real = httpx.URL(unwrap_trackers(item.audio_url)).host
        if real:
            vistos.add(f"→ {real}")
    inspection.trackers = sorted(vistos)
    return inspection
