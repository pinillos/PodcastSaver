"""Etapa 4a: emisión del artefacto Markdown (§7.2).

Un fichero por episodio. Es la fuente de verdad y el contrato entre las dos
mitades del sistema: todo lo de aguas abajo se puede rehacer desde aquí sin
volver a transcribir.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .segments import Block, Segment, group_into_blocks, render_body, slugify

SCHEMA_VERSION = 2
TRANSCRIPTS_DIR = Path("transcripts")


@dataclass
class ExportInput:
    podcast: str
    podcast_slug: str
    episode_title: str
    published_at: str
    language: str
    audio_url: str
    guid: str
    segments: list[Segment]

    authors: list[str] = field(default_factory=list)
    episode_number: int | None = None
    duration_sec: int | None = None
    transcribed_duration_sec: int | None = None
    episode_url: str | None = None
    checksum_audio: str | None = None

    engine: str = "whisper.cpp"
    model: str = "ggml-large-v3-turbo-q5_0"
    glossary: str | None = None
    vad: bool = True
    diarized: bool = False
    needs_review: bool = False
    review_reason: str | None = None
    transcribed_at: str | None = None

    enrichment: dict[str, Any] | None = None
    chapters: list[dict] | None = None
    chapters_source: str = "none"
    source: str = "rss"


def episode_filename(published_at: str, title: str, guid: str | None = None) -> str:
    """Nombre de fichero estable y único.

    El slug se recorta a 60 caracteres, así que dos títulos largos que
    empiecen igual —«Analizamos a fondo el nuevo modelo que presenta X» vs
    «…que presenta Y»— producían el mismo nombre y un episodio sobrescribía
    al otro en silencio. Se añade un sufijo del guid cuando hay riesgo.
    """
    base = f"{published_at[:10]}-{slugify(title)}"
    if guid and len(slugify(title, max_length=1000)) > 60:
        base += "-" + hashlib.sha256(guid.encode("utf-8")).hexdigest()[:6]
    return base + ".md"


def md_path_for(
    podcast_slug: str,
    published_at: str,
    title: str,
    *,
    guid: str | None = None,
    root: Path = TRANSCRIPTS_DIR,
) -> Path:
    return Path(root) / podcast_slug / episode_filename(published_at, title, guid)


def build_front_matter(data: ExportInput) -> dict:
    """Front matter YAML del §7.2.

    Los checksums de reindexado NO van aquí: se calculan al indexar y viven en
    Postgres (§8.5). Meter en el front matter un hash del propio front matter
    sería autorreferencial.
    """
    fm: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "podcast": data.podcast,
        "podcast_slug": data.podcast_slug,
    }
    if data.authors:
        fm["authors"] = list(data.authors)
    fm["episode_title"] = data.episode_title
    if data.episode_number is not None:
        fm["episode_number"] = data.episode_number
    fm["guid"] = data.guid
    fm["published_at"] = data.published_at
    if data.duration_sec is not None:
        fm["duration_sec"] = data.duration_sec
    if data.transcribed_duration_sec is not None:
        # No es lo mismo que duration_sec: la publicidad dinámica hace que
        # cambien entre sí, y su diferencia estima la deriva (§9.1).
        fm["transcribed_duration_sec"] = data.transcribed_duration_sec
    fm["language"] = data.language
    fm["audio_url"] = data.audio_url
    if data.episode_url:
        fm["episode_url"] = data.episode_url

    transcript: dict[str, Any] = {
        "engine": data.engine,
        "model": data.model,
        "transcribed_at": data.transcribed_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vad": data.vad,
        "diarized": data.diarized,
        "needs_review": data.needs_review,
    }
    if data.glossary:
        transcript["glossary"] = data.glossary
    if data.review_reason:
        transcript["review_reason"] = data.review_reason
    fm["transcript"] = transcript

    if data.enrichment:
        fm["enrichment"] = data.enrichment
    fm["chapters_source"] = data.chapters_source
    if data.chapters:
        fm["chapters"] = data.chapters
    fm["source"] = data.source
    if data.checksum_audio:
        # De la copia transcrita. NO es un detector de ediciones del autor:
        # con publicidad dinámica cambia en cada descarga (§9.1).
        fm["checksum_audio"] = data.checksum_audio
    return fm


def render_markdown(data: ExportInput, blocks: list[Block] | None = None) -> str:
    blocks = blocks if blocks is not None else group_into_blocks(data.segments)
    front_matter = yaml.safe_dump(
        build_front_matter(data), allow_unicode=True, sort_keys=False, width=100
    )

    titulo = data.episode_title
    if data.episode_number is not None:
        titulo += f" (Ep. {data.episode_number})"

    return (
        f"---\n{front_matter}---\n\n"
        f"# {titulo}\n\n"
        f"## Transcripción\n\n"
        f"{render_body(blocks)}\n"
    )


def split_front_matter(markdown: str) -> tuple[str, str]:
    """Separa front matter y cuerpo, que tienen ciclos de vida distintos (§8.5)."""
    if not markdown.startswith("---\n"):
        return "", markdown
    end = markdown.find("\n---\n", 4)
    if end == -1:
        return "", markdown
    return markdown[4:end + 1], markdown[end + 5:]


def transcript_checksum(markdown: str) -> str:
    """Hash del CUERPO: decide re-chunk + re-embed (§8.5)."""
    _, body = split_front_matter(markdown)
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()


def meta_checksum(markdown: str) -> str:
    """Hash del FRONT MATTER: decide un UPDATE barato sobre episodes (§8.5)."""
    front_matter, _ = split_front_matter(markdown)
    return hashlib.sha256(front_matter.strip().encode("utf-8")).hexdigest()


def write_episode(data: ExportInput, *, root: Path = TRANSCRIPTS_DIR) -> Path:
    path = md_path_for(
        data.podcast_slug, data.published_at, data.episode_title, guid=data.guid, root=root
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(data), encoding="utf-8")
    return path
