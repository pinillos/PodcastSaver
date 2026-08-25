"""Fragmentación para el índice (§8.1).

La unidad recuperable es un tramo de 1–2 minutos: suficiente para que un
embedding capture una idea, y corto como para que saltar a su `start_sec` te
deje justo donde se dice lo que buscabas.

Tres reglas del diseño se respetan aquí:
  - nunca se corta un segmento de whisper por la mitad;
  - hay solape para no partir una idea entre dos chunks;
  - si el episodio trae capítulos, las fronteras se alinean con ellos.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .segments import Segment

TARGET_SEC = 90.0
MAX_SEC = 150.0
OVERLAP_RATIO = 0.20
MIN_CHARS = 40

# Un chunk se atribuye a un hablante solo si domina claramente; en una
# tertulia a tres voces, atribuir por mayoría simple sería ruido.
SPEAKER_DOMINANCE = 0.6


@dataclass
class Chunk:
    idx: int
    start_sec: float
    end_sec: float
    content: str
    speaker: str | None = None
    chapter: str | None = None
    segment_count: int = 0

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


@dataclass
class ChunkingStats:
    n_chunks: int = 0
    mean_sec: float = 0.0
    mean_words: float = 0.0
    with_speaker: int = 0
    with_chapter: int = 0
    boundaries_from_chapters: int = 0
    extra: dict = field(default_factory=dict)


def _dominant_speaker(segments: list[Segment]) -> str | None:
    if not any(s.speaker for s in segments):
        return None
    weight: dict[str, int] = defaultdict(int)
    for segment in segments:
        if segment.speaker:
            weight[segment.speaker] += len(segment.text)
    if not weight:
        return None
    total = sum(len(s.text) for s in segments) or 1
    best, chars = max(weight.items(), key=lambda kv: kv[1])
    return best if chars / total >= SPEAKER_DOMINANCE else None


def _clean_chapters(chapters: list[dict] | None) -> list[dict]:
    """Descarta capítulos sin `start` y los ordena.

    Un índice escrito a mano puede venir desordenado o incompleto; asumir lo
    contrario costaba un KeyError o etiquetas equivocadas.
    """
    limpios = []
    for chapter in chapters or []:
        start = chapter.get("start")
        if start is None:
            continue
        try:
            limpios.append({"start": float(start), "title": chapter.get("title")})
        except (TypeError, ValueError):
            continue
    return sorted(limpios, key=lambda c: c["start"])


def _chapter_at(chapters: list[dict], start: float) -> str | None:
    title = None
    for chapter in chapters:
        if chapter["start"] <= start:
            title = chapter.get("title")
        else:
            break
    return title


def _usable_boundaries(chapters: list[dict], min_gap: float) -> set[float]:
    """Fronteras de capítulo lo bastante separadas para no fragmentar el índice.

    Un episodio con un capítulo cada 10 s produciría chunks de 10 s: inútiles
    para recuperar (un embedding de una frase no captura nada) y multiplicando
    por diez el tamaño del índice. Se respetan las fronteras que caen a más de
    `min_gap` de la anterior aceptada.
    """
    usables: set[float] = set()
    ultima = None
    for chapter in chapters:
        if ultima is None or chapter["start"] - ultima >= min_gap:
            usables.add(chapter["start"])
            ultima = chapter["start"]
    return usables


def chunk_segments(
    segments: list[Segment],
    *,
    chapters: list[dict] | None = None,
    target_sec: float = TARGET_SEC,
    max_sec: float = MAX_SEC,
    overlap_ratio: float = OVERLAP_RATIO,
) -> list[Chunk]:
    """Agrupa segmentos en chunks solapados."""
    segments = sorted(
        (s for s in segments if s.text.strip()), key=lambda s: (s.start, s.end)
    )
    if not segments:
        return []

    chapters = _clean_chapters(chapters)
    # La frontera de capítulo manda, pero no por debajo de medio chunk.
    boundaries = _usable_boundaries(chapters, target_sec / 2)
    chunks: list[Chunk] = []
    start_index = 0

    while start_index < len(segments):
        buffer: list[Segment] = []
        index = start_index
        origin = segments[start_index].start
        cerrado_por_capitulo = False

        while index < len(segments):
            segment = segments[index]
            elapsed = segment.end - origin

            # Un capítulo nuevo empieza aquí: cerrar el chunk para que las
            # fronteras coincidan con las del autor (§8.1).
            if buffer and segment.start in boundaries:
                cerrado_por_capitulo = True
                break
            if buffer and elapsed > max_sec:
                break
            buffer.append(segment)
            index += 1
            if segment.end - origin >= target_sec:
                break

        if not buffer:
            break

        content = " ".join(s.text for s in buffer).strip()
        if content and (len(content) >= MIN_CHARS or not chunks):
            chunks.append(
                Chunk(
                    idx=len(chunks),
                    start_sec=buffer[0].start,
                    end_sec=buffer[-1].end,
                    content=content,
                    speaker=_dominant_speaker(buffer),
                    chapter=_chapter_at(chapters, buffer[0].start),
                    segment_count=len(buffer),
                )
            )
        elif chunks:
            # Una cola demasiado corta se pega al chunk anterior en vez de
            # quedarse como un fragmento inútil en el índice.
            previous = chunks[-1]
            previous.content = f"{previous.content} {content}".strip()
            previous.end_sec = buffer[-1].end
            previous.segment_count += len(buffer)

        if index >= len(segments):
            break

        if cerrado_por_capitulo:
            # Un capítulo es frontera dura: solapar a través de ella partiría
            # el chunk siguiente en un trozo inútil y mezclaría dos temas.
            start_index = index
            continue

        # Retroceder para solapar, sin quedarse nunca parado.
        span = buffer[-1].end - buffer[0].start
        overlap_target = span * overlap_ratio
        back = index - 1
        while back > start_index and segments[index - 1].end - segments[back].start < overlap_target:
            back -= 1
        start_index = max(start_index + 1, back)

    return chunks


def describe(chunks: list[Chunk]) -> ChunkingStats:
    if not chunks:
        return ChunkingStats()
    return ChunkingStats(
        n_chunks=len(chunks),
        mean_sec=sum(c.duration_sec for c in chunks) / len(chunks),
        mean_words=sum(len(c.content.split()) for c in chunks) / len(chunks),
        with_speaker=sum(1 for c in chunks if c.speaker),
        with_chapter=sum(1 for c in chunks if c.chapter),
    )
