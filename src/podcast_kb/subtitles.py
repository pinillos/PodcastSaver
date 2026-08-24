"""Importación de subtítulos publicados por el feed (§4.4).

Cuando un feed declara `<podcast:transcript>` en un formato con marcas de
tiempo, la transcripción ya está hecha: convertirla a nuestros segmentos
cuesta milisegundos y ahorra minutos de GPU por episodio. Para el corpus
actual son 148 h de audio que no hay que pasar por Whisper.

Se soportan WebVTT y SubRip, que es lo que publican los hostings.
"""

from __future__ import annotations

import html
import re

from .segments import Segment

# 00:01:02.345 (VTT) o 00:01:02,345 (SRT); las horas son opcionales en VTT.
_TIME = r"(?:(\d{1,3}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
_CUE_RE = re.compile(rf"^\s*{_TIME}\s*-->\s*{_TIME}\s*(?P<settings>.*)$")

# <v Antonio Ortiz>texto</v> — el hablante, gratis (§5.11).
_VOICE_RE = re.compile(r"<v\.?[^\s>]*\s+([^>]+)>", re.I)
_TAG_RE = re.compile(r"</?[a-z][^>]*>", re.I)
_SRT_INDEX_RE = re.compile(r"^\d+$")

# Bloques de WebVTT que no son diálogo.
_SKIP_BLOCK_RE = re.compile(r"^\s*(WEBVTT|NOTE|STYLE|REGION)\b", re.I)


class SubtitleError(ValueError):
    pass


def _to_seconds(hours: str | None, minutes: str, seconds: str, fraction: str) -> float:
    total = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
    return total + int(fraction.ljust(3, "0")) / 1000.0


def _clean(text: str) -> tuple[str, str | None]:
    """Devuelve (texto limpio, hablante si venía en una etiqueta <v>)."""
    speaker = None
    voice = _VOICE_RE.search(text)
    if voice:
        speaker = voice.group(1).strip() or None
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip(), speaker


def parse_subtitles(raw: str | bytes) -> list[Segment]:
    """Convierte WebVTT o SubRip en segmentos.

    Detecta el formato por el contenido, no por la extensión ni por el
    `type` del feed: los hostings los etiquetan de forma inconsistente
    (Cuonda publica el mismo fichero como `text/srt` y `application/x-subrip`).
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig", errors="replace")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")

    segments: list[Segment] = []
    pending_time: tuple[float, float] | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal pending_time
        if pending_time is None:
            return
        text, speaker = _clean(" ".join(buffer))
        if text:
            segments.append(Segment(pending_time[0], pending_time[1], text, speaker))
        buffer.clear()
        pending_time = None

    for line in raw.split("\n"):
        match = _CUE_RE.match(line)
        if match:
            flush()
            g = match.groups()
            pending_time = (_to_seconds(*g[0:4]), _to_seconds(*g[4:8]))
            continue

        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if pending_time is None:
            # Fuera de un cue solo hay cabeceras, identificadores y números
            # de orden de SRT; nada de eso es diálogo.
            continue
        if _SKIP_BLOCK_RE.match(stripped) or _SRT_INDEX_RE.match(stripped):
            continue
        buffer.append(stripped)

    flush()

    if not segments:
        raise SubtitleError("no se reconoció ningún subtítulo con marcas de tiempo")

    # Un fichero bien formado avanza; si no, la conversión no es fiable.
    segments.sort(key=lambda s: s.start)
    return segments


def looks_timestamped(mimetype: str | None) -> bool:
    """Si el `type` del feed promete marcas de tiempo."""
    return (mimetype or "").lower() in {
        "text/vtt",
        "application/x-subrip",
        "text/srt",
        "application/srt",
    }
