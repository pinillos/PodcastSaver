"""Capítulos declarados por el autor en las show notes (§6).

El §6 del diseño dice "preferir los del autor si existen". Muchos podcasts no
usan `<podcast:chapters>` pero sí listan marcas de tiempo en la descripción:

    00:00:00 - Introducción
    00:01:16 - Probarse ropa con IA
    00:03:21 - DeepMind midió si la IA puede manipularte

Extraerlas cuesta un regex y ahorra una pasada de LLM, además de dar
fronteras reales para alinear los chunks (§8.1).
"""

from __future__ import annotations

import html
import re

# Marca de tiempo al principio de línea (o tras una etiqueta HTML), separador,
# y el título hasta el final de la línea.
_CHAPTER_RE = re.compile(
    r"""(?m)
    (?:^|>|\n)\s*
    (?P<ts>(?:\d{1,2}:)?\d{1,2}:\d{2})
    \s*(?:[-–—:·|]|\)|\.)\s+
    (?P<title>[^<\n\r]{2,120}?)
    \s*(?=<|\n|\r|$)
    """,
    re.X,
)

_TAG_RE = re.compile(r"<[^>]+>")

# Con menos de esto, casi seguro que son menciones sueltas y no un índice.
MIN_CHAPTERS = 3


def _to_seconds(timestamp: str) -> int | None:
    parts = timestamp.split(":")
    if len(parts) > 3:
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 for n in nums):
        return None
    if len(nums) >= 2 and any(n > 59 for n in nums[1:]):
        return None
    total = 0
    for n in nums:
        total = total * 60 + n
    return total


def extract_chapters(description: str | None, *, duration_sec: int | None = None) -> list[dict]:
    """Capítulos a partir de la descripción. Lista vacía si no los hay.

    Es deliberadamente conservador: prefiere no devolver nada a devolver un
    índice inventado, porque un capítulo falso desplaza las fronteras de
    chunk y empeora la búsqueda.
    """
    if not description:
        return []

    text = html.unescape(description)
    # Un <br> o </p> hace de salto de línea a efectos de índice.
    text = re.sub(r"(?i)<\s*(br|/p|/div|/li)\s*/?>", "\n", text)

    found: list[dict] = []
    for match in _CHAPTER_RE.finditer(text):
        seconds = _to_seconds(match.group("ts"))
        if seconds is None:
            continue
        title = _TAG_RE.sub("", match.group("title")).strip(" -–—:·|\t")
        title = html.unescape(title).strip()
        if not title:
            continue
        found.append({"start": seconds, "title": title})

    if len(found) < MIN_CHAPTERS:
        return []

    # Un índice real avanza. Si no, lo que hemos encontrado son menciones
    # sueltas de minutos y no un índice.
    monotonic: list[dict] = []
    for chapter in found:
        if monotonic and chapter["start"] <= monotonic[-1]["start"]:
            continue
        monotonic.append(chapter)

    if len(monotonic) < MIN_CHAPTERS:
        return []
    if duration_sec and monotonic[-1]["start"] > duration_sec:
        return []  # se sale del episodio: no era un índice
    return monotonic
