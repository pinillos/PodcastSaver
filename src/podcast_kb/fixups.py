"""Normalización post-Whisper (§5.7, §5.8).

El prompt inicial condiciona sobre todo las primeras ventanas de 30 s; su
efecto decae. La palanca de consistencia a lo largo del episodio es esta
tabla, que además es auditable en Git.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .paths import config_path
from .segments import Segment


def default_fixups_path() -> Path:
    return config_path("fixups.tsv")


@dataclass
class Fixup:
    pattern: re.Pattern[str]
    replacement: str
    note: str = ""
    raw: str = ""


def load_fixups(path: Path | str | None = None) -> list[Fixup]:
    path = Path(path) if path is not None else default_fixups_path()
    if not path.exists():
        return []

    fixups: list[Fixup] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        pattern_src = parts[0].strip()
        if not pattern_src:
            continue
        replacement = parts[1] if len(parts) > 1 else ""
        note = parts[2].strip() if len(parts) > 2 else ""
        try:
            compiled = re.compile(pattern_src, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"{path}:{lineno}: patrón inválido {pattern_src!r}: {exc}") from exc
        fixups.append(Fixup(compiled, replacement, note, pattern_src))
    return fixups


def apply_fixups(segments: list[Segment], fixups: list[Fixup]) -> tuple[list[Segment], int]:
    """Aplica los reemplazos. Devuelve los segmentos y cuántos cambios hubo.

    Un reemplazo vacío borra la coincidencia — así se limpian las frases
    fantasma de §5.8. Si un segmento queda vacío, desaparece.
    """
    out: list[Segment] = []
    changes = 0
    for segment in segments:
        text = segment.text
        for fixup in fixups:
            text, n = fixup.pattern.subn(fixup.replacement, text)
            changes += n
        text = re.sub(r"\s{2,}", " ", text).strip()
        if text:
            out.append(Segment(segment.start, segment.end, text, segment.speaker))
    return out, changes


def strip_prompt_echo(segments: list[Segment], prompt: str, *, threshold: float = 0.6) -> bool:
    """Descarta el primer segmento si Whisper repitió el prompt (§5.7).

    Devuelve True si se descartó algo. El prompt es contexto previo simulado:
    a veces el modelo lo continúa en vez de condicionarse con él.
    """
    if not segments or not prompt.strip():
        return False

    prompt_words = set(re.findall(r"\w+", prompt.lower()))
    if not prompt_words:
        return False

    first_words = re.findall(r"\w+", segments[0].text.lower())
    if len(first_words) < 4:
        return False

    overlap = sum(1 for w in first_words if w in prompt_words) / len(first_words)
    if overlap >= threshold:
        segments.pop(0)
        return True
    return False
