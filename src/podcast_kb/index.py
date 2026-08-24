"""Etapa 4b: cargar los .md en Postgres (§8).

Esta etapa lee **solo el repositorio**: no toca SQLite ni el audio. Es lo que
permite que corra en un GitHub Action con el Mac apagado (§3.2), y lo que hace
que reindexar sea barato y retranscribir caro (§3.1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from . import chunking, segments as seg_mod
from .embeddings import Embedder, to_pgvector
from .export import TRANSCRIPTS_DIR, meta_checksum, split_front_matter, transcript_checksum


class Cursor(Protocol):  # pragma: no cover - solo para tipar
    def execute(self, sql: str, params: Any = ...) -> Any: ...
    def fetchone(self) -> Any: ...


@dataclass
class EpisodeDocument:
    md_path: Path
    front_matter: dict
    body: str
    transcript_checksum: str
    meta_checksum: str

    @property
    def podcast_slug(self) -> str:
        return self.front_matter["podcast_slug"]

    @property
    def guid(self) -> str:
        return self.front_matter["guid"]

    @property
    def segments_path(self) -> Path:
        return self.md_path.parent / (self.md_path.stem + seg_mod.SEGMENTS_SUFFIX)


@dataclass
class IndexReport:
    scanned: int = 0
    inserted: int = 0
    metadata_only: int = 0
    unchanged: int = 0
    chunks: int = 0
    embedded: int = 0
    cached: int = 0
    errors: list[str] = field(default_factory=list)


def load_document(md_path: Path | str) -> EpisodeDocument:
    md_path = Path(md_path)
    text = md_path.read_text(encoding="utf-8")
    raw_front_matter, body = split_front_matter(text)
    front_matter = yaml.safe_load(raw_front_matter) or {}
    for required in ("podcast_slug", "guid", "published_at", "language"):
        if required not in front_matter:
            raise ValueError(f"{md_path}: falta `{required}` en el front matter")
    return EpisodeDocument(
        md_path=md_path,
        front_matter=front_matter,
        body=body,
        transcript_checksum=transcript_checksum(text),
        meta_checksum=meta_checksum(text),
    )


def discover(root: Path | str = TRANSCRIPTS_DIR) -> list[Path]:
    return sorted(Path(root).glob("*/*.md"))


def chunks_for(document: EpisodeDocument) -> list[chunking.Chunk]:
    """Chunks a partir de los segmentos crudos, no del Markdown.

    El .md agrupa en bloques legibles para humanos; el chunking del índice
    tiene otro criterio. Partir del JSON permite rehacerlo sin retranscribir
    (§7.2), que es justo para lo que se conserva.
    """
    if document.segments_path.exists():
        segments = seg_mod.load_segments(document.segments_path)
    else:
        segments = _segments_from_markdown(document.body)
    return chunking.chunk_segments(segments, chapters=document.front_matter.get("chapters"))


def _segments_from_markdown(body: str) -> list[seg_mod.Segment]:
    """Reconstrucción aproximada cuando falta el .segments.json.gz."""
    import re

    pattern = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]\s*(?:\(([^)]+)\)\s*)?(.+)$")
    out: list[seg_mod.Segment] = []
    for line in body.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        h, m, s, speaker, text = match.groups()
        start = int(h) * 3600 + int(m) * 60 + int(s)
        if out:
            out[-1].end = float(start)
        out.append(seg_mod.Segment(float(start), float(start), text.strip(), speaker))
    if out:
        out[-1].end = out[-1].start + 60.0
    return out


def _upsert_podcast(cur: Cursor, document: EpisodeDocument) -> str:
    front_matter = document.front_matter
    authors = front_matter.get("authors") or []
    if isinstance(authors, str):
        authors = [a.strip() for a in authors.split(",") if a.strip()]
    cur.execute(
        """
        insert into podcasts (slug, title, authors, language)
        values (%s, %s, %s, %s)
        on conflict (slug) do update
          set title = excluded.title,
              authors = excluded.authors,
              language = excluded.language
        returning id
        """,
        (
            document.podcast_slug,
            front_matter.get("podcast") or document.podcast_slug,
            authors,
            front_matter.get("language", "es"),
        ),
    )
    return cur.fetchone()[0]


def _upsert_episode(cur: Cursor, podcast_id: str, document: EpisodeDocument) -> tuple[str, bool]:
    """Devuelve (episode_id, hace_falta_rechunkear)."""
    front_matter = document.front_matter
    enrichment = front_matter.get("enrichment") or {}
    cur.execute(
        "select id, transcript_checksum from episodes where podcast_id = %s and guid = %s",
        (podcast_id, document.guid),
    )
    existing = cur.fetchone()

    cur.execute(
        """
        insert into episodes (
            podcast_id, guid, title, episode_number, published_at, duration_sec,
            transcribed_duration_sec, audio_url, episode_url, language,
            summary, topics, entities, keywords, chapters, chapters_source,
            needs_review, transcript_checksum, meta_checksum, indexed_at
        ) values (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()
        )
        on conflict (podcast_id, guid) do update set
            title = excluded.title,
            episode_number = excluded.episode_number,
            published_at = excluded.published_at,
            duration_sec = excluded.duration_sec,
            transcribed_duration_sec = excluded.transcribed_duration_sec,
            audio_url = excluded.audio_url,
            episode_url = excluded.episode_url,
            language = excluded.language,
            summary = excluded.summary,
            topics = excluded.topics,
            entities = excluded.entities,
            keywords = excluded.keywords,
            chapters = excluded.chapters,
            chapters_source = excluded.chapters_source,
            needs_review = excluded.needs_review,
            transcript_checksum = excluded.transcript_checksum,
            meta_checksum = excluded.meta_checksum,
            indexed_at = now()
        returning id
        """,
        (
            podcast_id,
            document.guid,
            front_matter.get("episode_title") or "(sin título)",
            front_matter.get("episode_number"),
            front_matter["published_at"],
            front_matter.get("duration_sec"),
            front_matter.get("transcribed_duration_sec"),
            front_matter.get("audio_url") or "",
            front_matter.get("episode_url"),
            front_matter.get("language", "es"),
            enrichment.get("summary"),
            enrichment.get("topics"),
            enrichment.get("entities"),
            enrichment.get("keywords"),
            json.dumps(front_matter.get("chapters"), ensure_ascii=False)
            if front_matter.get("chapters")
            else None,
            front_matter.get("chapters_source", "none"),
            bool((front_matter.get("transcript") or {}).get("needs_review")),
            document.transcript_checksum,
            document.meta_checksum,
        ),
    )
    episode_id = cur.fetchone()[0]
    # Solo el cuerpo decide re-chunk + re-embed; el front matter cambia cada
    # vez que se re-enriquece y no debe invalidar 17.000 vectores (§8.5).
    rechunk = existing is None or existing[1] != document.transcript_checksum
    return episode_id, rechunk


def index_document(
    cur: Cursor,
    document: EpisodeDocument,
    embedder: Embedder,
    *,
    report: IndexReport,
    force: bool = False,
    cache: dict[str, list[float]] | None = None,
) -> None:
    podcast_id = _upsert_podcast(cur, document)
    episode_id, rechunk = _upsert_episode(cur, podcast_id, document)

    if not rechunk and not force:
        report.metadata_only += 1
        return

    pieces = chunks_for(document)
    if not pieces:
        report.errors.append(f"{document.md_path}: sin segmentos que indexar")
        return

    cache = cache if cache is not None else {}
    textos = [c.content for c in pieces]

    # Reutilizar los vectores que ya están en la base de datos (B.4). Al
    # reajustar el chunking o retranscribir con un fixup, la mayor parte del
    # texto sigue siendo idéntica; re-embeder todo sería pagar dos veces por
    # lo mismo —tiempo de GPU, o dinero si el embedder es de pago—.
    pendientes = {t for t in textos if t not in cache}
    if pendientes:
        cur.execute(
            "select content, embedding from chunks where episode_id = %s and content = any(%s)",
            (episode_id, list(pendientes)),
        )
        for content, embedding in cur.fetchall():
            if embedding is not None:
                cache[content] = embedding

    cur.execute("delete from chunks where episode_id = %s", (episode_id,))

    faltan = [t for t in textos if t not in cache]
    if faltan:
        for texto, vector in zip(faltan, embedder.embed_passages(faltan)):
            cache[texto] = to_pgvector(vector)
    report.embedded += len(faltan)
    report.cached += len(textos) - len(faltan)

    front_matter = document.front_matter
    for chunk in pieces:
        cur.execute(
            """
            insert into chunks (
                episode_id, idx, start_sec, end_sec, speaker, content,
                podcast_id, published_at, language, embedding
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                episode_id,
                chunk.idx,
                int(chunk.start_sec),
                int(chunk.end_sec),
                chunk.speaker,
                chunk.content,
                podcast_id,
                front_matter["published_at"],
                front_matter.get("language", "es"),
                cache[chunk.content],
            ),
        )

    report.inserted += 1
    report.chunks += len(pieces)


def index_all(
    cur: Cursor,
    embedder: Embedder,
    *,
    root: Path | str = TRANSCRIPTS_DIR,
    force: bool = False,
) -> IndexReport:
    report = IndexReport()
    cache: dict[str, list[float]] = {}
    for md_path in discover(root):
        report.scanned += 1
        try:
            document = load_document(md_path)
        except (ValueError, yaml.YAMLError) as exc:
            report.errors.append(f"{md_path}: {exc}")
            continue
        index_document(cur, document, embedder, report=report, force=force, cache=cache)
    report.unchanged = report.metadata_only
    return report
