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
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

from .urls import enclosure_sha256

USER_AGENT = "podcast-kb/0.1 (+base de conocimiento personal; contacto en el repo)"

ITUNES_LOOKUP = "https://itunes.apple.com/lookup"

PODCAST_NS = "https://podcastindex.org/namespace/1.0"
PSC_NS = "http://podlove.org/simple-chapters"

_EP_NUM_RE = re.compile(r"(?:^|[#\s])(?:ep(?:isodio|isode)?\.?\s*)?(\d{1,4})\b", re.I)

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
    n_chapters: int = 0
    trackers: list[str] = field(default_factory=list)


@dataclass
class FeedResult:
    title: str | None
    language: str | None
    items: list[EpisodeItem] = field(default_factory=list)
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
) -> tuple[bytes | None, str | None, str | None]:
    """Descarga el feed usando caché condicional (§4.2).

    Devuelve (contenido, etag, last_modified). El contenido es None si el
    servidor responde 304: no hay nada nuevo que parsear.
    """
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    owns_client = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=True)
    try:
        resp = client.get(rss_url, headers=headers)
    finally:
        if owns_client:
            client.close()

    # §12: respetar el rate limiting que pida el servidor.
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After", "?")
        raise FeedError(f"429 del servidor; Retry-After={retry_after}")
    if resp.status_code == 304:
        return None, etag, last_modified
    resp.raise_for_status()
    return resp.content, resp.headers.get("ETag"), resp.headers.get("Last-Modified")


def _to_iso(struct_time: Any) -> str | None:
    if not struct_time:
        return None
    try:
        dt = datetime(*struct_time[:6], tzinfo=timezone.utc)
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
    return int(match.group(1)) if match else None


def _podcasting20_by_guid(raw_xml: bytes) -> dict[str, dict]:
    """Elementos <podcast:*> por guid, que feedparser no expone (§4.4)."""
    found: dict[str, dict] = {}
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return found

    for item in root.iter("item"):
        guid_el = item.find("guid")
        guid = (guid_el.text or "").strip() if guid_el is not None else ""
        if not guid:
            continue
        data: dict[str, Any] = {}

        transcript = item.find(f"{{{PODCAST_NS}}}transcript")
        if transcript is not None and transcript.get("url"):
            data["feed_transcript_url"] = transcript.get("url")
            data["feed_transcript_type"] = transcript.get("type")

        chapters = item.find(f"{{{PODCAST_NS}}}chapters")
        if chapters is not None and chapters.get("url"):
            data["feed_chapters_url"] = chapters.get("url")
        elif item.find(f"{{{PSC_NS}}}chapters") is not None:
            data["feed_chapters_url"] = "psc:inline"

        if data:
            found[guid] = data
    return found


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


def parse_feed(raw_xml: bytes) -> FeedResult:
    parsed = feedparser.parse(raw_xml)
    extras = _podcasting20_by_guid(raw_xml)

    channel = parsed.feed
    result = FeedResult(
        title=channel.get("title"),
        language=(channel.get("language") or "").split("-")[0].lower() or None,
    )

    for entry in parsed.entries:
        audio_url = ""
        audio_bytes = None
        for enclosure in entry.get("enclosures") or []:
            href = enclosure.get("href") or enclosure.get("url") or ""
            if href:
                audio_url = href
                try:
                    audio_bytes = int(enclosure.get("length") or 0) or None
                except (TypeError, ValueError):
                    audio_bytes = None
                break
        if not audio_url:
            continue  # sin audio no hay episodio que transcribir

        title = entry.get("title") or "(sin título)"
        guid = _entry_guid(entry, audio_url)
        published = _to_iso(entry.get("published_parsed")) or _to_iso(
            entry.get("updated_parsed")
        )
        if not published:
            continue  # published_at es NOT NULL: sin fecha no se da de alta

        extra = extras.get(guid, {})
        result.items.append(
            EpisodeItem(
                guid=guid,
                title=title,
                published_at=published,
                audio_url=audio_url,
                enclosure_sha256=enclosure_sha256(audio_url),
                duration_sec=parse_duration(entry.get("itunes_duration")),
                audio_bytes=audio_bytes,
                description=entry.get("summary"),
                episode_url=entry.get("link"),
                episode_number=extract_episode_number(title, entry),
                feed_transcript_url=extra.get("feed_transcript_url"),
                feed_transcript_type=extra.get("feed_transcript_type"),
                feed_chapters_url=extra.get("feed_chapters_url"),
            )
        )
    return result


def inspect_feed(rss_url: str, *, client: httpx.Client | None = None) -> FeedInspection:
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
        parsed = parse_feed(resp.content)
    except httpx.HTTPError as exc:
        inspection.error = f"{type(exc).__name__}: {exc}"
        return inspection
    except Exception as exc:  # noqa: BLE001 - el diagnóstico no debe romper el flujo
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
    inspection.n_chapters = sum(1 for i in parsed.items if i.feed_chapters_url)

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
