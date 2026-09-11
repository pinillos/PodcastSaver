"""Segmentos de transcripción: el artefacto crudo del que sale todo (§5.12, §7.2).

El `.md` es para humanos y para indexar; este JSON es la materia prima, y se
conserva comprimido para poder rehacer el chunking sin retranscribir.
"""

from __future__ import annotations

import gzip
import json
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

SEGMENTS_SUFFIX = ".segments.json.gz"

# Bloques de ~30-60 s para legibilidad humana (§7.2).
BLOCK_TARGET_SEC = 45.0
BLOCK_MAX_SEC = 75.0


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None

    def as_dict(self) -> dict:
        data = asdict(self)
        if data["speaker"] is None:
            del data["speaker"]
        return data


@dataclass
class Block:
    """Agrupación de segmentos crudos para el cuerpo del Markdown."""

    start: float
    text: str
    speaker: str | None = None


def format_timestamp(seconds: float) -> str:
    """`[HH:MM:SS]`, siempre absoluto desde el inicio del audio (§7.2)."""
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def parse_whisper_cpp_json(payload: dict) -> list[Segment]:
    """Salida de `whisper-cli -oj`: offsets en milisegundos."""
    out: list[Segment] = []
    for item in payload.get("transcription") or []:
        offsets = item.get("offsets") or {}
        text = (item.get("text") or "").strip()
        if not text:
            continue
        out.append(
            Segment(
                start=float(offsets.get("from", 0)) / 1000.0,
                end=float(offsets.get("to", 0)) / 1000.0,
                text=text,
            )
        )
    return out


def parse_openai_whisper_json(payload: dict) -> list[Segment]:
    """Salida de mlx-whisper / openai-whisper: `segments` con segundos."""
    out: list[Segment] = []
    for item in payload.get("segments") or []:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        out.append(
            Segment(
                start=float(item.get("start", 0.0)),
                end=float(item.get("end", 0.0)),
                text=text,
            )
        )
    return out


def parse_transcript_json(payload: dict) -> list[Segment]:
    """Detecta el formato por su forma, en vez de por el motor que lo produjo."""
    if "transcription" in payload:
        return parse_whisper_cpp_json(payload)
    if "segments" in payload:
        return parse_openai_whisper_json(payload)
    raise ValueError("JSON de transcripción no reconocido: falta 'transcription' o 'segments'")


def save_segments(segments: list[Segment], path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, "segments": [s.as_dict() for s in segments]}
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return path


def load_segments(path: Path | str) -> list[Segment]:
    with gzip.open(Path(path), "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    return [Segment(**item) for item in payload["segments"]]


def group_into_blocks(
    segments: list[Segment],
    *,
    target_sec: float = BLOCK_TARGET_SEC,
    max_sec: float = BLOCK_MAX_SEC,
) -> list[Block]:
    """Agrupa segmentos crudos en bloques legibles, sin partir ninguno.

    El timestamp del bloque es el `start` del primer segmento (§7.2). Un
    cambio de hablante siempre corta el bloque: mezclarlos haría ilegible la
    transcripción diarizada.
    """
    blocks: list[Block] = []
    buffer: list[Segment] = []

    def flush() -> None:
        if not buffer:
            return
        blocks.append(
            Block(
                start=buffer[0].start,
                text=" ".join(s.text for s in buffer).strip(),
                speaker=buffer[0].speaker,
            )
        )
        buffer.clear()

    for segment in segments:
        if buffer:
            cambia_hablante = segment.speaker != buffer[0].speaker
            duracion = segment.end - buffer[0].start
            if cambia_hablante or duracion > max_sec or segment.start - buffer[0].start >= target_sec:
                flush()
        buffer.append(segment)

    flush()
    return blocks


def render_body(blocks: list[Block]) -> str:
    """Cuerpo Markdown de la transcripción (§7.2)."""
    lines = []
    for block in blocks:
        prefijo = f"[{format_timestamp(block.start)}]"
        if block.speaker:
            prefijo += f" ({block.speaker})"
        lines.append(f"{prefijo} {block.text}")
    return "\n\n".join(lines)


def slugify(text: str, *, max_length: int = 60) -> str:
    """Nombre de fichero ASCII, en minúsculas y sin espacios (§7.2)."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    ascii_text = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    if len(ascii_text) > max_length:
        ascii_text = ascii_text[:max_length].rsplit("-", 1)[0]
    return ascii_text or "sin-titulo"


def detect_repetition(
    segments: list[Segment], *, n: int = 6, threshold: int = 5
) -> tuple[bool, str | None]:
    """Detector de bucles de repetición de Whisper (§5.8).

    Sin esto, un episodio que degeneró en una frase repetida se publica sin
    que nadie se entere salvo leyéndolo entero.
    """
    words: list[str] = []
    for segment in segments:
        words.extend(re.findall(r"\w+", segment.text.lower()))
    if len(words) < n:
        return False, None

    ngrams = Counter(tuple(words[i : i + n]) for i in range(len(words) - n + 1))
    ngram, count = ngrams.most_common(1)[0]
    if count >= threshold:
        return True, f"{count}× «{' '.join(ngram)}»"
    return False, None
